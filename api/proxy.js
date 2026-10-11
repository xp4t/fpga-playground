// A private server-side boundary: client headers never select the upstream or secret.
const MAX_BODY = 210000;
const MAX_RESPONSE = 128 * 1024 * 1024;

function upstreamURL(path) {
  const base = new URL(process.env.BACKEND_URL);
  if (base.protocol !== 'https:' || base.username || base.password) throw new Error('Invalid backend URL');
  const match = path.match(/^\/api\/(?:health|session|source|status|board|control|synthesize|implement|bitstream|testbench|jobs\/[A-Za-z0-9_-]+|artifact\/[A-Za-z0-9_.-]+)$/);
  if (!match) return null;
  base.pathname = path;
  base.search = '';
  base.hash = '';
  return base;
}

async function bodyBytes(request) {
  const declared = Number(request.headers['content-length'] || 0);
  if (declared > MAX_BODY) throw new RangeError('Source request is too large');
  // Vercel may parse JSON before invoking a Node function.
  if (request.body !== undefined) {
    const body = Buffer.isBuffer(request.body) ? request.body : Buffer.from(
      typeof request.body === 'string' ? request.body : JSON.stringify(request.body));
    if (body.length > MAX_BODY) throw new RangeError('Source request is too large');
    return body;
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY) throw new RangeError('Source request is too large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

module.exports = async function proxy(request, response) {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('CDN-Cache-Control', 'no-store');
  response.setHeader('Vary', 'Cookie');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  const fail = (status, error) => {
    response.statusCode = status;
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({error}));
  };
  if (!['GET', 'HEAD', 'POST'].includes(request.method)) return fail(405, 'Method not allowed');
  try {
    const parsed = new URL(request.url, 'http://localhost');
    // Internal rewrite passes the original API path; /api/proxy cannot choose an arbitrary host.
    const path = parsed.pathname === '/api/proxy' ? parsed.searchParams.get('route') : parsed.pathname;
    if (!process.env.BACKEND_URL || !process.env.BACKEND_SECRET) return fail(503, 'FPGA backend not configured');
    const url = upstreamURL(path || '');
    if (!url) return fail(404, 'Not found');
    const headers = {
      'X-Backend-Secret': process.env.BACKEND_SECRET,
      // Vercel overwrites X-Forwarded-For with the caller IP. Never forward client X-Build-Client-IP.
      'X-Build-Client-IP': String(request.headers['x-forwarded-for'] || request.socket?.remoteAddress || 'unknown').split(',')[0].trim(),
      'X-Forwarded-Proto': 'https'
    };
    for (const name of ['cookie', 'origin', 'sec-fetch-site', 'content-type']) {
      if (request.headers[name]) headers[name] = request.headers[name];
    }
    const body = request.method === 'POST' ? await bodyBytes(request) : undefined;
    const upstream = await fetch(url, {method: request.method, headers, body,
      signal: AbortSignal.timeout(8000), redirect: 'error', cache: 'no-store'});
    response.statusCode = upstream.status;
    for (const name of ['content-type', 'content-disposition', 'retry-after']) {
      const value = upstream.headers.get(name);
      if (value) response.setHeader(name, value);
    }
    const cookies = upstream.headers.getSetCookie ? upstream.headers.getSetCookie() : upstream.headers.get('set-cookie');
    if (cookies && (!Array.isArray(cookies) || cookies.length)) response.setHeader('Set-Cookie', cookies);
    if (request.method === 'HEAD') return response.end();
    // Stream artifacts with backpressure; never buffer a bitstream in the function heap.
    let size = 0;
    if (upstream.body) {
      for await (const chunk of upstream.body) {
        size += chunk.length;
        if (size > MAX_RESPONSE) throw new Error('Response too large');
        if (!response.write(chunk)) await new Promise(resolve => response.once('drain', resolve));
      }
    }
    response.end();
  } catch (error) {
    if (response.headersSent) return response.destroy();
    return fail(error instanceof RangeError ? 413 : 503,
      error instanceof RangeError ? error.message : 'FPGA server unavailable. You can keep editing and retry shortly.');
  }
};
