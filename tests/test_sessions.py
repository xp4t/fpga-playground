"""Two independent HTTP clients must never share RTL, state, or artifacts."""
import http.cookiejar
import json
import secrets
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import server


class Client:
    def __init__(self, base):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, payload=None, headers=None):
        headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers.get_content_type() == 'application/json':
                body = json.loads(body)
            return response.status, body, response.headers

    def token(self):
        return next(cookie.value for cookie in self.cookies if cookie.name == server.SESSION_COOKIE)


class SessionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = server.SessionStore(Path(self.temp.name) / 'sessions')
        self.patch = mock.patch.object(server, 'SESSIONS', self.store)
        self.patch.start()
        self.tools = mock.patch.object(server, 'openxc7_paths', return_value=None)
        self.tools.start()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'
        self.a, self.b = Client(self.base), Client(self.base)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        for entry in self.store.entries.values():
            entry['lab'].close()
        self.patch.stop()
        self.tools.stop()
        self.temp.cleanup()

    def test_bootstrap_private_cookie_and_no_anonymous_access(self):
        status, body, headers = self.a.request('/api/session')
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True})
        cookie = headers['Set-Cookie']
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, cookie)
        self.assertNotIn('Domain=', cookie)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['CDN-Cache-Control'], 'no-store')
        self.assertEqual(headers['Vary'], 'Cookie')
        token = self.a.token()
        self.assertIsNone(self.a.request('/api/session')[2].get('Set-Cookie'))
        self.assertEqual(self.a.token(), token)
        self.assertEqual(self.b.request('/api/status')[0], 401)
        self.assertEqual(self.b.request('/api/artifact/design.v')[0], 401)
        self.assertEqual(self.b.request('/api/control', {'step': 1})[0], 401)
        self.assertEqual(self.b.request('/api/source')[1]['code'], server.DEFAULT_CODE)
        self.assertNotEqual(self.a.token(), self.b.token())
        outsider = Client(self.base)
        self.assertEqual(outsider.request('/api/status', headers={
            'Cookie': f'{server.SESSION_COOKIE}={secrets.token_urlsafe(32)}'})[0], 401)
        self.assertEqual(outsider.request('/api/session', headers={
            'X-Forwarded-Proto': 'https'})[0], 200)
        self.assertIn('Secure', outsider.request('/api/session', headers={
            'X-Forwarded-Proto': 'https'})[2]['Set-Cookie'])

    def test_cross_origin_reads_and_mutations_rejected(self):
        self.a.request('/api/session')
        for path in ('/api/source', '/api/status', '/api/artifact/design.v', '/api/session'):
            self.assertEqual(self.a.request(path, headers={'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(self.a.request(path, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.a.request('/api/board', {'board': 'arty_a7_100t'},
                                       {'Origin': 'https://evil.example'})[0], 403)

    @unittest.skipUnless(all(shutil.which(name) for name in ('yosys', 'iverilog', 'vvp', 'bwrap')),
                         'FPGA tools and Bubblewrap required')
    def test_concurrent_builds_artifacts_failures_and_controls_are_isolated(self):
        for client in (self.a, self.b):
            client.request('/api/session')
        code_a = "module alice_secret(input [15:0] sw, output [15:0] led); assign led=sw ^ 16'hAAAA; endmodule"
        code_b = "module bob_secret(input [15:0] sw, output [15:0] led); assign led=sw ^ 16'h5555; endmodule"
        with ThreadPoolExecutor(max_workers=2) as pool:
            builds = [pool.submit(client.request, '/api/synthesize', {'code': code, 'top': top})
                      for client, code, top in ((self.a, code_a, 'alice_secret'), (self.b, code_b, 'bob_secret'))]
            for result in builds:
                status, body, _ = result.result()
                self.assertEqual(status, 200, body)
            previews = [pool.submit(client.request, '/api/implement', {'switches': 0}) for client in (self.a, self.b)]
            results = [job.result() for job in previews]
        self.assertEqual(results[0][1]['leds'], 'aaaa')
        self.assertEqual(results[1][1]['leds'], '5555')
        for client, own, other in ((self.a, code_a, 'bob_secret'), (self.b, code_b, 'alice_secret')):
            self.assertEqual(client.request('/api/source')[1]['code'], own)
            self.assertEqual(client.request('/api/artifact/design.v')[1], own.encode())
            status, body, _ = client.request('/api/status')
            self.assertEqual(status, 200)
            self.assertNotIn(other, json.dumps(body))
            self.assertNotIn(other.encode(), client.request('/api/artifact/netlist.json')[1])
        before_b = self.b.request('/api/status')[1]
        self.assertEqual(self.a.request('/api/control', {'switches': 1})[1]['leds'], 'aaab')
        self.assertEqual(self.b.request('/api/status')[1]['cycles'], before_b['cycles'])
        self.assertEqual(self.b.request('/api/status')[1]['leds'], '5555')
        self.assertEqual(self.a.request('/api/synthesize', {'code': 'invalid RTL', 'top': 'x'})[0], 400)
        self.assertEqual(self.b.request('/api/artifact/design.v')[1], code_b.encode())
        self.assertEqual(self.b.request('/api/status')[1]['phase'], 'preview')
        self.a.request('/api/board', {'board': 'arty_a7_100t'})
        self.assertEqual(self.b.request('/api/status')[1]['board'], 'basys3')
        with self.store.acquire(self.b.token()) as (lab, _):
            other_dir = lab.work.name
        for name in (f'../{other_dir}/design.v', f'%2e%2e/{other_dir}/design.v', '/etc/passwd'):
            self.assertEqual(self.a.request('/api/artifact/' + name)[0], 404)

    @unittest.skipUnless(shutil.which('bwrap') and shutil.which('yosys'), 'Sandbox and synthesis required')
    def test_hdl_cannot_read_another_workspace_or_host_file(self):
        self.a.request('/api/session')
        self.b.request('/api/session')
        with self.store.acquire(self.a.token()) as (lab_a, _), self.store.acquire(self.b.token()) as (lab_b, _):
            secret = lab_a.work / 'design.v'
            secret.write_text('module TOP_SECRET_MARKER(output y); assign y=1; endmodule')
            own = lab_b.work / 'own.txt'
            own.write_text('own data')
            self.assertEqual(lab_b.run(['/bin/cat', str(own)]), 'own data')
            with self.assertRaises(ValueError):
                lab_b.run(['/bin/cat', str(secret)])
            host_secret = Path(self.temp.name) / 'host-secret.txt'
            host_secret.write_text('HOST_PRIVATE_MARKER')
            with self.assertRaises(ValueError):
                lab_b.run(['/bin/cat', str(host_secret)])
            lab_b.work.joinpath('design.v').symlink_to(secret)
        self.assertEqual(self.b.request('/api/artifact/design.v')[0], 404)
        # Remove our deliberate test symlink before asking the compiler to write its input.
        lab_b.work.joinpath('design.v').unlink()
        status, body, _ = self.b.request('/api/synthesize', {'code': f'`include "{secret}"', 'top': 'test'})
        self.assertEqual(status, 400)
        self.assertNotIn('TOP_SECRET_MARKER', json.dumps(body))
        self.assertIn('TOP_SECRET_MARKER', self.a.request('/api/artifact/design.v')[1].decode())


class SessionLifecycleTests(unittest.TestCase):
    def test_expiry_capacity_and_inflight_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.SessionStore(Path(directory), ttl=10, limit=1)
            with mock.patch.object(server.time, 'monotonic', return_value=0) as clock:
                with store.acquire(None, create=True) as (lab, token):
                    old_work = lab.work
                    clock.return_value = 20
                    with self.assertRaises(server.SessionError) as error:
                        with store.acquire(None, create=True):
                            pass
                    self.assertEqual(error.exception.status, 503)
                    self.assertTrue(old_work.exists())
                clock.return_value = 31
                with self.assertRaises(server.SessionError):
                    with store.acquire(token):
                        pass
                self.assertFalse(old_work.exists())
                with store.acquire(token, create=True) as (fresh, fresh_token):
                    self.assertNotEqual(fresh_token, token)
                    self.assertEqual(fresh.code, server.DEFAULT_CODE)
                    self.assertNotEqual(fresh.work, old_work)

    def test_missing_sandbox_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = server.Lab(directory)
            with mock.patch.object(server.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(ValueError, 'Bubblewrap'):
                    lab.run(['/bin/true'])
