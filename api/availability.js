const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const config = require('../vercel.json');

// Use the same destination as the API rewrite, so tunnel changes have one source.
const upstream = config.rewrites.find(route => route.source === '/api/:path*').destination;
const healthURL = upstream.replace('/api/:path*', '/api/health');
const home = readFileSync(join(process.cwd(), 'web/index.html'));
const offline = readFileSync(join(process.cwd(), 'web/503.html'));

module.exports = async function availability(request, response) {
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  if (!['GET', 'HEAD'].includes(request.method)) {
    response.setHeader('Allow', 'GET, HEAD');
    response.statusCode = 405;
    return response.end();
  }
  let online = false;
  try {
    const result = await fetch(healthURL, {
      signal: AbortSignal.timeout(4000), cache: 'no-store', redirect: 'error'
    });
    online = result.ok && (await result.json()).ok === true;
  } catch {
    // A powered-off PC, stopped server, and disconnected tunnel are unavailable.
  }
  response.statusCode = online ? 200 : 503;
  if (!online) response.setHeader('Retry-After', '30');
  const check = new URL(request.url, 'http://localhost').searchParams.get('check') === '1';
  response.setHeader('Content-Type', check ? 'application/json; charset=utf-8' : 'text/html; charset=utf-8');
  return response.end(request.method === 'HEAD' ? undefined : check
    ? JSON.stringify({ok: online, ...(online ? {} : {error: 'FPGA server unavailable'})})
    : online ? home : offline);
};
