const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
function context() {
  const nodes = new Map();
  const sandbox = vm.createContext({
    document: {getElementById(id) {if (!nodes.has(id)) nodes.set(id, {textContent: '', hidden: false}); return nodes.get(id);}},
    setTimeout(callback) {callback();}, clearInterval() {}, AbortSignal
  });
  vm.runInContext(source.slice(0, source.lastIndexOf('\ninit().catch')), sandbox);
  vm.runInContext('render = () => {}; renderControls = () => {};', sandbox);
  return {sandbox, nodes};
}

test('poll queued and running progress until final snapshot, never resubmit a build', async () => {
  const {sandbox, nodes} = context();
  const urls = [];
  const stages = [
    {jobId: 'safe-id', status: 'running', log: 'routing', elapsedSeconds: 121},
    {jobId: 'safe-id', status: 'succeeded', result: {phase: 'implemented'}}
  ];
  sandbox.fetch = async url => {urls.push(url); return Response.json(stages.shift());};
  const result = await sandbox.monitorJob({jobId: 'safe-id', status: 'queued', queuePosition: 2});
  assert.equal(result.phase, 'implemented');
  assert.deepEqual(urls, ['/api/jobs/safe-id', '/api/jobs/safe-id']);
  assert.equal(nodes.get('build-log').textContent, 'routing');
  assert.equal(vm.runInContext('busy', sandbox), false);
  assert.equal(vm.runInContext('pollingJob', sandbox), null);
  // Terminal snapshots from reload must not leave a stale polling promise behind.
  await sandbox.monitorJob({jobId: 'done', status: 'succeeded', result: {phase: 'bitstream'}});
  assert.equal(vm.runInContext('pollingJob', sandbox), null);
});

test('poll retries a transient outage and reports terminal failures/restarts', async () => {
  const {sandbox} = context();
  let attempt = 0;
  sandbox.fetch = async () => {
    if (++attempt === 1) throw new TypeError('offline');
    return Response.json({jobId: 'id', status: 'failed', error: 'Time limit reached', result: {phase: 'ready'}});
  };
  await assert.rejects(sandbox.monitorJob({jobId: 'id', status: 'running'}), /Time limit reached/);
  assert.equal(attempt, 2);
  assert.equal(vm.runInContext('busy', sandbox), false);
  sandbox.fetch = async () => Response.json({error: 'Session expired'}, {status: 401});
  await assert.rejects(sandbox.monitorJob({jobId: 'old', status: 'running'}), /Session expired/);
});

test('first offline visit has local starter code and source/testbench downloads remain discoverable', () => {
  const html = fs.readFileSync('web/index.html', 'utf8');
  assert.match(html, /id="editor"[^>]*>[\s\S]*?module counter/);
  assert.match(html, /id="download-source"/);
  assert.match(html, /id="testbench-editor"[^>]*>[\s\S]*?\$dumpfile\("testbench.vcd"\)/);
  assert.match(source, /localStorage\.getItem\('fpga-workbench-private-code-v1'\)/);
});
