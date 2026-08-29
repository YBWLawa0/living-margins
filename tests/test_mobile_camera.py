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
        self.database = WebDatabase(root / "app.db")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), living_margins_web.create_handler(self.database)
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

    def test_two_users_have_separate_frame_and_state_channels(self) -> None:
        status, first_user = self.request_json(
            "/api/auth/register", {"username": "camera-one", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, first_started = self.request_json("/api/reading/start", {"device_id": None})
        self.assertEqual(status, 201)
        self.assertEqual(self.upload(b"\xff\xd8one\xff\xd9")[0], 202)

        self.cookie = None
        status, second_user = self.request_json(
            "/api/auth/register", {"username": "camera-two", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, second_started = self.request_json("/api/reading/start", {"device_id": None})
        self.assertEqual(status, 201)
        self.assertEqual(self.upload(b"\xff\xd8two\xff\xd9")[0], 202)

        first_session = int(first_started["reading_session"]["id"])
        second_session = int(second_started["reading_session"]["id"])
        self.assertNotEqual(first_session, second_session)
        self.assertEqual(
            {path.name for path in self.frames.glob("*.jpg")},
            {
                f"user-{first_user['user']['id']}-session-{first_session}.jpg",
                f"user-{second_user['user']['id']}-session-{second_session}.jpg",
            },
        )

        states = self.frames.parent / "states"
        states.mkdir()
        now = __import__("time").time()
        for session_id, book_id in ((first_session, "book-one"), (second_session, "book-two")):
            (states / f"session-{session_id}.json").write_text(
                json.dumps(
                    {
                        "revision": session_id,
                        "status": "stable",
                        "book_id": book_id,
                        "pages": [10, 11],
                        "_relay_received_at": now,
                    }
                ),
                encoding="utf-8",
            )
        with patch.object(living_margins_web, "VISION_SOURCE", "relay"), patch.object(
            living_margins_web, "RELAY_STATE_ROOT", states
        ):
            first_state = living_margins_web.current_vision_state(
                int(first_user["user"]["id"]), self.database
            )
            second_state = living_margins_web.current_vision_state(
                int(second_user["user"]["id"]), self.database
            )
        self.assertEqual(first_state["book_id"], "book-one")
        self.assertEqual(second_state["book_id"], "book-two")

if __name__ == "__main__":
    unittest.main()
