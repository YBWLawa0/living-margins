from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import living_margins_web
from library_terra.web_database import WebDatabase


class MobileCameraTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.frames = root / "frames"
        self.path_patcher = patch.object(living_margins_web, "MOBILE_FRAME_ROOT", self.frames)
        self.path_patcher.start()
        database = WebDatabase(root / "app.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), living_margins_web.create_handler(database)
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie: str | None = None

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.path_patcher.stop()
        self.temporary.cleanup()

    def request_json(self, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request("POST", path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        connection.close()
        return response.status, payload

    def upload(self, payload: bytes, content_type: str = "image/jpeg") -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Content-Type": content_type}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request("POST", "/api/vision/frame", payload, headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def register_and_start(self) -> None:
        status, _ = self.request_json(
            "/api/auth/register", {"username": "camera", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, _ = self.request_json("/api/reading/start", {"device_id": None})
        self.assertEqual(status, 201)

    def test_upload_requires_login_and_active_reading(self) -> None:
        jpeg = b"\xff\xd8frame\xff\xd9"
        status, _ = self.upload(jpeg)
        self.assertEqual(status, 401)
        status, _ = self.request_json(
            "/api/auth/register", {"username": "camera", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, response = self.upload(jpeg)
        self.assertEqual(status, 409)
        self.assertIn("开始阅读", response["error"])

    def test_upload_replaces_latest_frame(self) -> None:
        self.register_and_start()
        first = b"\xff\xd8first\xff\xd9"
        second = b"\xff\xd8second\xff\xd9"
        self.assertEqual(self.upload(first)[0], 202)
        status, response = self.upload(second)
        self.assertEqual(status, 202)
        self.assertEqual(response["bytes"], len(second))
        files = list(self.frames.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), second)

    def test_upload_rejects_invalid_jpeg(self) -> None:
        self.register_and_start()
        status, response = self.upload(b"not-a-jpeg")
        self.assertEqual(status, 400)
        self.assertIn("JPEG", response["error"])


if __name__ == "__main__":
    unittest.main()
