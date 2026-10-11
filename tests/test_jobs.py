"""Exercise jobs over HTTP without sockets or external FPGA tools."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import server
from test_sessions import Client


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = server.SessionStore(Path(self.temp.name), ttl=0.1)
        self.jobs = server.JobStore(depth=1)
        self.patches = [mock.patch.object(server, 'SESSIONS', self.store),
                        mock.patch.object(server, 'JOBS', self.jobs),
                        mock.patch.dict(server.os.environ, {'BACKEND_SECRET': '', 'ALLOWED_ORIGINS': ''})]
        for patch in self.patches:
            patch.start()
        self.clients = [Client('memory:') for _ in range(4)]
        for client in self.clients:
            client.request('/api/session')
        self.started, self.release = threading.Event(), threading.Event()

    def tearDown(self):
        self.release.set()
        self.jobs.queue.join()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def slow_build(self, lab, *args):
        self.started.set()
        lab.progress('Yosys progress\n')
        if not self.release.wait(3):
            raise TimeoutError('Test blocked')
        lab.phase = 'synthesized'
        return lab.snapshot()

    def submit(self, client):
        return client.request('/api/synthesize', {'code': server.DEFAULT_CODE, 'top': 'counter'}, wait=False)

    def finish(self, client, job):
        for _ in range(200):
            status, body, _ = client.request('/api/jobs/' + job['jobId'])
            if body['status'] in ('failed', 'succeeded'):
                return status, body
            time.sleep(0.005)
        self.fail('Worker did not complete')

    def test_immediate_submit_progress_status_isolation_queue_and_session_pinning(self):
        a, b, c, d = self.clients
        with mock.patch.object(server.Lab, 'synthesize', autospec=True, side_effect=self.slow_build):
            before = time.monotonic()
            status, first, _ = self.submit(a)
            self.assertEqual(status, 202)
            self.assertLess(time.monotonic() - before, 0.2)
            self.assertTrue(self.started.wait(1))
            self.assertEqual(a.request('/api/jobs/' + first['jobId'])[1]['status'], 'running')
            self.assertIn('Yosys progress', a.request('/api/jobs/' + first['jobId'])[1]['log'])
            self.assertEqual(b.request('/api/jobs/' + first['jobId'])[0], 404)
            self.assertEqual(a.request('/api/status')[1]['job']['jobId'], first['jobId'])
            self.assertEqual(a.request('/api/health')[0], 200)
            self.assertEqual(self.submit(a)[0], 409)
            self.assertEqual(a.request('/api/board', {'board': 'arty_a7_100t'})[0], 409)
            self.assertEqual(a.request('/api/artifact/design.v')[0], 409)
            status, second, _ = self.submit(b)
            self.assertEqual(status, 202)
            self.assertEqual(second['status'], 'queued')
            self.assertEqual(second['queuePosition'], 1)
            self.assertEqual(self.submit(c)[0], 429)
            # Idle session expiration must not remove a queued or running lab.
            with self.store.lock:
                for entry in self.store.entries.values():
                    entry['seen'] -= 1
            d.request('/api/session')
            self.assertIn(self.store.key(a.token()), self.store.entries)
            self.assertIn(self.store.key(b.token()), self.store.entries)
            self.release.set()
            self.assertEqual(self.finish(a, first)[1]['status'], 'succeeded')
            self.assertEqual(self.finish(b, second)[1]['status'], 'succeeded')

    def test_failures_release_workspace_and_worker_continues(self):
        a, b, _, _ = self.clients
        with mock.patch.object(server.Lab, 'synthesize', side_effect=ValueError('invalid RTL')):
            _, job, _ = self.submit(a)
            _, result = self.finish(a, job)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error'], 'invalid RTL')
            self.assertEqual(a.request('/api/board', {'board': 'arty_a7_100t'})[0], 200)
        with mock.patch.object(server.Lab, 'synthesize', autospec=True, side_effect=lambda lab, *args: lab.snapshot()):
            _, job, _ = self.submit(b)
            self.assertEqual(self.finish(b, job)[1]['status'], 'succeeded')

    def test_source_cap_in_bytes_and_per_ip_rate_limit_across_cookies(self):
        a, b, _, _ = self.clients
        self.assertEqual(a.request('/api/synthesize', {'code': 'x' * 100001}, wait=False)[0], 400)
        self.assertEqual(a.request('/api/synthesize', {'code': 'λ' * 50001}, wait=False)[0], 400)
        with mock.patch.dict(server.os.environ, {'BUILDS_PER_MINUTE': '1'}), mock.patch.object(
                server.Lab, 'synthesize', autospec=True, side_effect=lambda lab, *args: lab.snapshot()):
            _, job, _ = self.submit(a)
            self.finish(a, job)
            self.assertEqual(self.submit(b)[0], 429)

    def test_restart_loses_old_jobs_without_revealing_another_workspace(self):
        self.assertEqual(self.clients[0].request('/api/jobs/unknown')[0], 404)


class AuthenticationTests(unittest.TestCase):
    def test_faked_origin_cannot_bypass_secret_on_any_api(self):
        client = Client('memory:')
        with mock.patch.dict(server.os.environ, {'BACKEND_SECRET': 'private', 'ALLOWED_ORIGINS': 'https://site.example'}):
            for path, payload in [('/api/session', None), ('/api/health', None), ('/api/status', None),
                                  ('/api/jobs/known', None), ('/api/artifact/design.bit', None),
                                  ('/api/synthesize', {'code': server.DEFAULT_CODE})]:
                for secret in ('', 'wrong'):
                    status, _, _ = client.request(path, payload, {'Origin': 'https://site.example', 'X-Backend-Secret': secret}, wait=False)
                    self.assertEqual(status, 403)
            self.assertEqual(client.request('/api/health', headers={'X-Backend-Secret': 'private'})[0], 200)

    def test_local_no_secret_mode_rejects_forwarded_tunnel_requests(self):
        with mock.patch.dict(server.os.environ, {'BACKEND_SECRET': '', 'ALLOWED_ORIGINS': ''}):
            for header in ('X-Forwarded-Proto', 'X-Forwarded-For', 'CF-Connecting-IP'):
                self.assertEqual(Client('memory:').request('/api/session', headers={header: 'fake'})[0], 403)
