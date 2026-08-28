from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import living_margins_web
from library_terra.web_database import DEMO_DEVICE_CODE, WebDatabase
from living_margins_web import create_handler


class WebServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vision_patcher = patch("living_margins_web.read_vision_state", return_value=None)
        self.vision_mock = self.vision_patcher.start()
        database = WebDatabase(Path(self.temporary.name) / "app.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(database))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.vision_patcher.stop()
        self.temporary.cleanup()

    def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key: value for key, value in response.getheaders()}
        connection.close()
        if "Set-Cookie" in response_headers:
            self.cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        return response.status, json.loads(raw), response_headers

    def test_auth_binding_and_reading_session_contract(self) -> None:
        status, _, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 401)

        status, registered, _ = self.request(
            "POST", "/api/auth/register", {"username": "reader", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(registered["user"]["username"], "reader")
        self.assertEqual(registered["user"]["role"], "admin")
        self.assertTrue(self.cookie)

        self.vision_mock.return_value = {
            "revision": 12,
            "book_id": "book-live",
            "title": "实时书籍",
            "pages": [40, 41],
            "status": "stable",
        }

        status, bound, _ = self.request(
            "POST", "/api/devices/bind", {"machine_code": DEMO_DEVICE_CODE}
        )
        self.assertEqual(status, 200)
        device_id = bound["device"]["id"]

        status, feedback, _ = self.request(
            "POST",
            "/api/device/feedback",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "comment_id": "comment-live-1",
                "book_id": "book-live",
                "page": 40,
                "action": "agree",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(feedback["outcome"], "created")

        status, duplicate_feedback, _ = self.request(
            "POST",
            "/api/device/feedback",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "comment_id": "comment-live-1",
                "book_id": "book-live",
                "page": 40,
                "action": "agree",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(duplicate_feedback["outcome"], "unchanged")

        status, changed_feedback, _ = self.request(
            "POST",
            "/api/device/feedback",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "comment_id": "comment-live-1",
                "book_id": "book-live",
                "page": 40,
                "action": "disagree",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed_feedback["outcome"], "changed")

        status, current_feedback, _ = self.request(
            "POST",
            "/api/device/feedback/current",
            {"machine_code": DEMO_DEVICE_CODE, "comment_id": "comment-live-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(current_feedback["feedback"]["action"], "disagree")

        status, started, _ = self.request(
            "POST", "/api/reading/start", {"device_id": device_id}
        )
        self.assertEqual(status, 201)
        self.assertEqual(started["reading_session"]["status"], "active")

        status, marked, _ = self.request("POST", "/api/inspirations/mark", {})
        self.assertEqual(status, 201)
        self.assertTrue(marked["created"])
        self.assertEqual(marked["inspiration"]["pages"], [40, 41])

        status, duplicate, _ = self.request("POST", "/api/inspirations/mark", {})
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["inspiration"]["id"], marked["inspiration"]["id"])

        status, converted, _ = self.request(
            "POST",
            "/api/inspirations/convert",
            {"inspiration_id": marked["inspiration"]["id"]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(converted["comment"]["status"], "draft")
        self.assertEqual(converted["comment"]["pages"], [40, 41])

        status, paused, _ = self.request("POST", "/api/reading/pause", {})
        self.assertEqual(status, 200)
        self.assertEqual(paused["reading_session"]["status"], "paused")

        status, drafted, _ = self.request(
            "POST", "/api/comments/draft", {"body": "这一处值得再想一想"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(drafted["comment"]["status"], "draft")
        self.assertEqual(drafted["comment"]["pages"], [40, 41])

        status, submitted, _ = self.request(
            "POST",
            "/api/comments/submit",
            {"comment_id": drafted["comment"]["id"], "body": "这一处值得再想一想"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(submitted["comment"]["status"], "pending")

        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("inspirations", bootstrap["capabilities"])
        self.assertIn("user_feedback", bootstrap["capabilities"])
        self.assertEqual(bootstrap["reading_session"]["status"], "paused")
        self.assertEqual(len(bootstrap["devices"]), 1)
        self.assertEqual(bootstrap["comments"][0]["status"], "pending")
        self.assertEqual(bootstrap["inspirations"], [])
        self.assertEqual(len(bootstrap["review_queue"]), 1)

        status, reviewed, _ = self.request(
            "POST",
            "/api/admin/comments/review",
            {"comment_id": drafted["comment"]["id"], "decision": "rejected"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["comment"]["status"], "rejected")

    def test_approved_comment_is_published_to_book_store(self) -> None:
        root = Path(self.temporary.name) / "books"
        book_dir = root / "book-live"
        book_dir.mkdir(parents=True)
        (book_dir / "book.json").write_text(
            json.dumps({"id": "book-live", "title": "实时书籍"}, ensure_ascii=False),
            encoding="utf-8",
        )
        published = living_margins_web.publish_approved_comment(
            {
                "id": 7,
                "book_id": "book-live",
                "pages": [40, 41],
                "body": "这一页值得再想一想",
                "author_username": "reader",
            },
            root,
        )
        self.assertEqual(published["id"], "web-comment-7")
        self.assertEqual(published["page"], 40)
        self.assertEqual(published["page_end"], 41)

        document = json.loads((book_dir / "comments.json").read_text(encoding="utf-8"))
        self.assertEqual(document["comments"][0]["author"], "reader")


class LiveVisionStateTests(unittest.TestCase):
    def test_returns_only_state_from_live_http_service(self) -> None:
        expected = {
            "revision": 8,
            "book_id": "book-live",
            "title": "实时书籍",
            "pages": [40, 41],
            "status": "stable",
        }
        response = Mock(status=200)
        response.read.return_value = json.dumps(expected, ensure_ascii=False).encode("utf-8")
        connection = Mock()
        connection.getresponse.return_value = response

        with patch("living_margins_web.HTTPConnection", return_value=connection):
            self.assertEqual(living_margins_web.read_vision_state(), expected)

        connection.request.assert_called_once_with(
            "GET", "/state", headers={"Accept": "application/json"}
        )
        connection.close.assert_called_once()

    def test_offline_service_does_not_fall_back_to_snapshot(self) -> None:
        connection = Mock()
        connection.request.side_effect = ConnectionRefusedError()

        with patch("living_margins_web.HTTPConnection", return_value=connection):
            self.assertIsNone(living_margins_web.read_vision_state())

        connection.close.assert_called_once()

    def test_unready_live_service_does_not_publish_old_state(self) -> None:
        response = Mock(status=503)
        response.read.return_value = b'{"error":"state_not_ready"}'
        connection = Mock()
        connection.getresponse.return_value = response

        with patch("living_margins_web.HTTPConnection", return_value=connection):
            self.assertIsNone(living_margins_web.read_vision_state())


if __name__ == "__main__":
    unittest.main()
