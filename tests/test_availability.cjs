const assert = require('node:assert/strict');
const {test} = require('node:test');
const handler = require('../api/availability.js');

async function request(url = '/', method = 'GET') {
  const response = {headers: {}, setHeader(name, value) {this.headers[name] = value;}, end(body) {this.body = body;}};
  await handler({url, method}, response);
  return response;
}

test('availability gate reports outages, recovers, and never caches an outage', async () => {
  const original = global.fetch;
  try {
    for (const outage of [
      () => {throw new TypeError('Connection refused');},
      () => {throw new DOMException('Timed out', 'TimeoutError');},
      () => new Response('<html>Cloudflare tunnel unavailable</html>', {status: 530}),
      () => new Response('not JSON'),
      () => Response.json({ok: false})
    ]) {
      global.fetch = outage;
      const page = await request();
      assert.equal(page.statusCode, 503);
      assert.match(page.body.toString(), /The FPGA server is offline/);
      assert.equal(page.headers['Retry-After'], '30');
      assert.equal(page.headers['Cache-Control'], 'no-store');
      const health = await request('/api/availability?check=1');
      assert.equal(health.statusCode, 503);
      assert.equal(JSON.parse(health.body).ok, false);
    }
    global.fetch = async (url, options) => {
      assert.ok(url.endsWith('/api/health'));
      assert.equal(options.cache, 'no-store');
      assert.ok(options.signal instanceof AbortSignal);
      return Response.json({ok: true});
    };
    const recovered = await request();
    assert.equal(recovered.statusCode, 200);
    assert.match(recovered.body.toString(), /id="editor"/);
    assert.equal(recovered.headers['Retry-After'], undefined);
    assert.equal(JSON.parse((await request('/api/availability?check=1')).body).ok, true);
    assert.equal((await request('/', 'HEAD')).body, undefined);
    assert.equal((await request('/', 'POST')).statusCode, 405);
  } finally { global.fetch = original; }
});
