// Verify API errors from a static host, sleeping service, and real backend.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(0, source.lastIndexOf('\ninit().catch')), context);

async function check() {
  context.fetch = async () => new Response('The page could not be found', {status: 404});
  await assert.rejects(context.requestJSON('/api/status'), /backend not connected/i);

  context.fetch = async () => new Response('<html>Starting service</html>', {
    status: 503, headers: {'Content-Type': 'text/html'}
  });
  await assert.rejects(context.requestJSON('/api/status'), /HTTP 503.*sleeping service/i);

  context.fetch = async () => new Response('{"phase":"ready"}', {
    headers: {'Content-Type': 'application/json; charset=utf-8'}
  });
  assert.equal((await context.requestJSON('/api/status')).phase, 'ready');

  context.fetch = async () => new Response('{"error":"Cross-origin request rejected"}', {
    status: 403, headers: {'Content-Type': 'application/json'}
  });
  await assert.rejects(context.api('/api/board', {board: 'basys3'}), /Cross-origin request rejected/);

  context.fetch = async () => new Response('invalid', {headers: {'Content-Type': 'application/json'}});
  await assert.rejects(context.requestJSON('/api/status'), /invalid JSON.*HTTP 200/);

  context.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(context.requestJSON('/api/status'), /Cannot reach the FPGA backend/);
  console.log('API checks passed: missing route, unavailable service, valid JSON, backend error, invalid JSON, and network failure.');
}

check().catch(error => { console.error(error); process.exitCode = 1; });
