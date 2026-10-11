"""Health checks must bypass the workbench lock held by FPGA builds."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock
import server


class HealthTests(unittest.TestCase):
    def test_health_without_accessing_busy_lab(self):
        class Connection:
            def makefile(self, *args):
                return io.BytesIO(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            def sendall(self, data):
                output.extend(data)

        for path in ("/api/health", "/healthz"):
            with self.subTest(path=path):
                output = bytearray()
                # No session methods exist: health must not create or inspect a lab.
                with mock.patch.object(server, 'SESSIONS', SimpleNamespace()):
                    server.Handler(Connection(), ('127.0.0.1', 12345), SimpleNamespace())
                headers, body = bytes(output).split(b'\r\n\r\n', 1)
                self.assertIn(b'200 OK', headers)
                self.assertIn(b'Cache-Control: no-store', headers)
                self.assertEqual(json.loads(body), {'ok': True})
