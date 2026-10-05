"""Exercise origin validation through HTTP requests without opening sockets."""

import io
import json
import threading
import unittest
from types import SimpleNamespace
from contextlib import contextmanager
from unittest import mock

import server


class MemorySocket:
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, mode, buffering=None):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


class OriginTests(unittest.TestCase):
    def post_board(self, origin, allowed=""):
        payload = b'{"board":"basys3"}'
        headers = ["POST /api/board HTTP/1.1", "Host: backend.example.com",
                   "Content-Type: application/json", f"Content-Length: {len(payload)}"]
        if origin is not None:
            headers.append(f"Origin: {origin}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode() + payload
        connection = MemorySocket(request)
        lab = SimpleNamespace(lock=threading.RLock(),
                              select_board=mock.Mock(return_value={"board": "basys3"}))
        @contextmanager
        def acquire(*args, **kwargs):
            yield lab, None
        with mock.patch.object(server, "SESSIONS", SimpleNamespace(acquire=acquire)), mock.patch.dict(
                server.os.environ, {"ALLOWED_ORIGINS": allowed}):
            server.Handler(connection, ("127.0.0.1", 12345), SimpleNamespace())
        header, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        status = int(header.split(b" ", 2)[1])
        return status, json.loads(body), lab.select_board

    def test_local_same_host_remains_allowed(self):
        status, _, action = self.post_board("https://backend.example.com")
        self.assertEqual(status, 200)
        action.assert_called_once_with("basys3")

    def test_vercel_origin_needs_explicit_configuration(self):
        status, result, action = self.post_board("https://frontend.vercel.app")
        self.assertEqual(status, 403)
        self.assertIn("Cross-origin", result["error"])
        action.assert_not_called()

    def test_configured_vercel_origin_passes_proxy_host_mismatch(self):
        status, _, action = self.post_board(
            "https://frontend.vercel.app",
            " https://frontend.vercel.app/, https://preview.vercel.app ")
        self.assertEqual(status, 200)
        action.assert_called_once_with("basys3")

    def test_unlisted_scheme_or_host_is_rejected(self):
        for origin in ("http://frontend.vercel.app", "https://frontend.vercel.app.evil.com",
                       "https://unlisted-preview.vercel.app", "null"):
            with self.subTest(origin=origin):
                status, _, action = self.post_board(origin, "https://frontend.vercel.app")
                self.assertEqual(status, 403)
                action.assert_not_called()

    def test_non_browser_clients_can_omit_origin(self):
        status, _, action = self.post_board(None)
        self.assertEqual(status, 200)
        action.assert_called_once_with("basys3")
