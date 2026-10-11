const assert = require('node:assert/strict');
const {test} = require('node:test');
const handler = require('../api/availability.js');
const config = require('../vercel.json');
async function request(method = 'GET') {
  const response = {headers: {}, setHeader(name, value) {this.headers[name] = value;}, end(body) {this.body = body;}};
  await handler({url: '/healthz', method}, response);
  return response;
}

test('static editor stays available while health reports outages and recovers', async () => {
  const original = global.fetch;
  const oldEnv = {...process.env};
  process.env.BACKEND_URL = 'https://build.example.com';
  process.env.BACKEND_SECRET = 'private-test-secret';
  try {
    assert.ok(!config.rewrites.some(route => ['/', '/index.html'].includes(route.source)));
    for (const outage of [
      () => {throw new TypeError('Connection refused');},
      () => {throw new DOMException('Timed out', 'TimeoutError');},
      () => new Response('<html>Cloudflare tunnel unavailable</html>', {status: 530}),
      () => new Response('not JSON'), () => Response.json({ok: false})
    ]) {
      global.fetch = outage;
      const health = await request();
      assert.equal(health.statusCode, 503);
      assert.equal(JSON.parse(health.body).ok, false);
      assert.equal(health.headers['Retry-After'], '30');
      assert.equal(health.headers['Cache-Control'], 'no-store');
      assert.ok(!health.body.includes('private-test-secret'));
    }
    global.fetch = async (url, options) => {
      assert.equal(url.href, 'https://build.example.com/api/health');
      assert.equal(options.headers['X-Backend-Secret'], 'private-test-secret');
      assert.equal(options.cache, 'no-store');
      assert.ok(options.signal instanceof AbortSignal);
      return Response.json({ok: true});
    };
    assert.equal((await request()).statusCode, 200);
    assert.equal((await request('HEAD')).body, undefined);
    assert.equal((await request('POST')).statusCode, 405);
    delete process.env.BACKEND_SECRET;
    assert.equal((await request()).statusCode, 503);
  } finally {global.fetch = original; process.env = oldEnv;}
});
