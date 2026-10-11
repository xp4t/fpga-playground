// Only health is gated. The homepage is a static editor, including during outages.
module.exports = async function availability(request, response) {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  if (!['GET', 'HEAD'].includes(request.method)) {
    response.statusCode = 405;
    response.setHeader('Allow', 'GET, HEAD');
    return response.end();
  }
  let online = false;
  try {
    if (!process.env.BACKEND_URL || !process.env.BACKEND_SECRET) throw new Error('Not configured');
    const url = new URL('/api/health', process.env.BACKEND_URL);
    if (url.protocol !== 'https:') throw new Error('Invalid backend URL');
    const result = await fetch(url, {
      headers: {'X-Backend-Secret': process.env.BACKEND_SECRET},
      signal: AbortSignal.timeout(4000), cache: 'no-store', redirect: 'error'
    });
    online = result.ok && (await result.json()).ok === true;
  } catch {}
  response.statusCode = online ? 200 : 503;
  if (!online) response.setHeader('Retry-After', '30');
  response.end(request.method === 'HEAD' ? undefined : JSON.stringify({ok: online}));
};
