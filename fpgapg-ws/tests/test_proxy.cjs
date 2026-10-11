const assert = require('node:assert/strict');
const {test} = require('node:test');
const handler = require('../api/proxy.js');

async function request(route = '/api/session', method = 'GET', body, headers = {}) {
  const response = {headers: {}, chunks: [], setHeader(k, v) {this.headers[k] = v;},
    write(chunk) {this.chunks.push(Buffer.from(chunk)); return true;},
    end(chunk) {if (chunk) this.chunks.push(Buffer.from(chunk));},
    destroy() {this.destroyed = true;}};
  await handler({url: '/api/proxy?route=' + encodeURIComponent(route), method, body,
    headers: {'x-forwarded-for': '203.0.113.8', ...headers}}, response);
  response.body = Buffer.concat(response.chunks).toString();
  return response;
}

test('proxy overwrites secret/IP, forwards sessions, streams artifacts, and fails closed', async () => {
  const original = global.fetch;
  const env = {...process.env};
  process.env.BACKEND_URL = 'https://build.example.com';
  process.env.BACKEND_SECRET = 'private-secret';
  try {
    global.fetch = async (url, options) => {
      assert.equal(url.href, 'https://build.example.com/api/synthesize');
      assert.equal(options.headers['X-Backend-Secret'], 'private-secret');
      assert.equal(options.headers['X-Build-Client-IP'], '203.0.113.8');
      assert.equal(options.headers.cookie, 'fpga_session=abc');
      assert.equal(options.headers.origin, 'https://frontend.example.com');
      assert.equal(options.headers['X-Forwarded-Proto'], 'https');
      assert.deepEqual(JSON.parse(options.body.toString()), {code: 'module a;endmodule'});
      return Response.json({jobId: 'job-id', status: 'queued'}, {status: 202, headers: {'Set-Cookie': 'fpga_session=abc; HttpOnly; Secure'}});
    };
    const response = await request('/api/synthesize', 'POST', {code: 'module a;endmodule'}, {
      'x-backend-secret': 'attacker', 'x-build-client-ip': 'fake',
      cookie: 'fpga_session=abc', origin: 'https://frontend.example.com', 'content-type': 'application/json'
    });
    assert.equal(response.statusCode, 202);
    assert.equal(JSON.parse(response.body).jobId, 'job-id');
    assert.ok(response.headers['Set-Cookie']);
    assert.equal(response.headers['Cache-Control'], 'no-store');
    assert.ok(!response.body.includes('private-secret'));
    global.fetch = async (url) => {
      assert.equal(url.pathname, '/api/artifact/design.bit');
      return new Response('binary artifact', {headers: {'Content-Disposition': 'attachment; filename="design.bit"'}});
    };
    assert.equal((await request('/api/artifact/design.bit')).body, 'binary artifact');
    for (const route of ['https://evil.example.com', '/api/../admin', '/api/artifact/../../secret', '/api/status?secret=1', '//evil/api/status']) {
      assert.equal((await request(route)).statusCode, 404);
    }
    assert.equal((await request('/api/synthesize', 'POST', {code: 'x'.repeat(210001)})).statusCode, 413);
    global.fetch = () => {throw new TypeError('backend unreachable');};
    assert.equal((await request()).statusCode, 503);
    delete process.env.BACKEND_SECRET;
    assert.equal((await request()).statusCode, 503);
  } finally {global.fetch = original; process.env = env;}
});
