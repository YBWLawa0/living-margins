from __future__ import annotations

import hashlib
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
        self.device_token = database.rotate_device_token(DEMO_DEVICE_CODE)
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
        if path.startswith("/api/device/feedback") and body is not None:
            body = dict(body)
            body.setdefault("device_token", self.device_token)
            payload = json.dumps(body).encode("utf-8")
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key: value for key, value in response.getheaders()}
        connection.close()
        if "Set-Cookie" in response_headers:
            self.cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        return response.status, json.loads(raw), response_headers

    def test_qr_pairing_contract(self) -> None:
        status, pairing, _ = self.request(
            "POST",
            "/api/device/pairing/start",
            {"machine_code": DEMO_DEVICE_CODE, "device_token": self.device_token},
        )
        self.assertEqual(status, 201)
        claim_token = pairing["pairing"]["pairing_token"]

        status, _, _ = self.request(
            "POST", "/api/auth/register", {"username": "scanner", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, claimed, _ = self.request(
            "POST", "/api/devices/pair/claim", {"pairing_token": claim_token}
        )
        self.assertEqual(status, 200)
        self.assertEqual(claimed["device"]["machine_code"], DEMO_DEVICE_CODE)
        status, duplicate, _ = self.request(
            "POST", "/api/devices/pair/claim", {"pairing_token": claim_token}
        )
        self.assertEqual(status, 400)
        self.assertIn("已经使用", duplicate["error"])

    def test_device_state_gateway_authenticates_and_relays_vision(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/auth/register", {"username": "relay", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "POST", "/api/devices/bind", {"machine_code": DEMO_DEVICE_CODE}
        )
        self.assertEqual(status, 200)
        self.vision_mock.return_value = {
            "revision": 21,
            "book_id": "relay-book",
            "title": "服务器转发",
            "pages": [8, 9],
            "status": "stable",
        }

        status, response, _ = self.request(
            "POST",
            "/api/device/state",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["vision"]["revision"], 21)
        self.assertTrue(response["changed"])

        status, unchanged, _ = self.request(
            "POST",
            "/api/device/state",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "revision": 21,
                "wait_ms": 0,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(unchanged["changed"])

        status, realtime, _ = self.request(
            "POST",
            "/api/device/state",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "revision": 21,
                "wait_ms": 1,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(realtime["changed"])

        status, presence, _ = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertTrue(presence["devices"][0]["online"])
        self.assertEqual(presence["devices"][0]["connection_mode"], "realtime")

        status, invalid_wait, _ = self.request(
            "POST",
            "/api/device/state",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "revision": 21,
                "wait_ms": 10001,
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("等待时间", invalid_wait["error"])

        status, response, _ = self.request(
            "POST",
            "/api/device/state",
            {"machine_code": DEMO_DEVICE_CODE, "device_token": "wrong"},
        )
        self.assertEqual(status, 401)
        self.assertIn("令牌", response["error"])
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

    def test_authenticated_reader_can_feedback_on_current_comment(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/auth/register", {"username": "web-reader", "password": "secret12"}
        )
        self.assertEqual(status, 200)
        self.vision_mock.return_value = {
            "revision": 13,
            "book_id": "book-live",
            "title": "实时书籍",
            "pages": [40, 41],
            "status": "stable",
            "comment": {
                "id": "comment-live-2",
                "page": 40,
                "text": "纸页上的另一种声音",
                "author": "另一位读者",
            },
        }

        status, created, _ = self.request(
            "POST",
            "/api/feedback",
            {"comment_id": "comment-live-2", "action": "agree"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["feedback"]["action"], "agree")

        status, changed, _ = self.request(
            "POST",
            "/api/feedback",
            {"comment_id": "comment-live-2", "action": "disagree"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(changed["outcome"], "changed")

        status, bootstrap, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["feedback"]), 1)
        self.assertEqual(bootstrap["feedback"][0]["action"], "disagree")

        status, rejected, _ = self.request(
            "POST",
            "/api/feedback",
            {"comment_id": "another-comment", "action": "agree"},
        )
        self.assertEqual(status, 400)
        self.assertIn("当前书页", rejected["error"])

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


class FirmwareOtaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temp = Path(self.temporary.name)
        self.release_root = temp / "firmware"
        self.release_root.mkdir()
        self.release_manifest = self.release_root / "release.json"
        self.release_root_patcher = patch.object(
            living_margins_web, "FIRMWARE_RELEASE_ROOT", self.release_root
        )
        self.release_manifest_patcher = patch.object(
            living_margins_web, "FIRMWARE_RELEASE_MANIFEST", self.release_manifest
        )
        self.release_root_patcher.start()
        self.release_manifest_patcher.start()

        database = WebDatabase(temp / "app.db")
        self.device_token = database.rotate_device_token(DEMO_DEVICE_CODE)
        owner = database.create_user("owner", "secret12")
        database.bind_device(owner["id"], DEMO_DEVICE_CODE)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(database))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.release_manifest_patcher.stop()
        self.release_root_patcher.stop()
        self.temporary.cleanup()

    def _write_release(
        self,
        *,
        version: str = "0.9.1",
        payload: bytes = b"firmware-body",
        manifest_version: str | None = None,
        manifest_size: int | None = None,
        manifest_sha256: str | None = None,
        manifest_binary: str = "firmware.bin",
        write_binary: bool = True,
    ) -> dict[str, object]:
        binary_path = self.release_root / "firmware.bin"
        if write_binary:
            binary_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        manifest = {
            "version": manifest_version if manifest_version is not None else version,
            "size": manifest_size if manifest_size is not None else len(payload),
            "sha256": manifest_sha256 if manifest_sha256 is not None else digest,
            "binary": manifest_binary,
        }
        self.release_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return {"payload": payload, "sha256": digest, "manifest": manifest}

    def _post(self, path: str, body: dict[str, object]) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(
            "POST",
            path,
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read()
        headers = {key: value for key, value in response.getheaders()}
        connection.close()
        return response.status, json.loads(raw), headers

    def _download(self, body: dict[str, object]) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(
            "POST",
            "/api/device/firmware/download",
            body=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read()
        headers = {key: value for key, value in response.getheaders()}
        connection.close()
        return response.status, raw, headers

    def test_check_offers_upgrade_when_release_is_newer(self) -> None:
        release = self._write_release(version="0.9.1", payload=b"payload-091")
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["available"])
        self.assertEqual(response["release"]["version"], "0.9.1")
        self.assertEqual(response["release"]["size"], len(release["payload"]))
        self.assertEqual(response["release"]["sha256"], release["sha256"])
        self.assertNotIn("path", response["release"])

    def test_check_reports_no_update_when_versions_match(self) -> None:
        self._write_release(version="0.9.1")
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.1",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["available"])
        self.assertIsNone(response["release"])

    def test_check_does_not_offer_downgrade(self) -> None:
        self._write_release(version="0.9.0", payload=b"older")
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.1",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["available"])
        self.assertIsNone(response["release"])

    def test_check_rejects_invalid_device_token(self) -> None:
        self._write_release(version="0.9.1")
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": "wrong",
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 401)
        self.assertIn("令牌", response["error"])

    def test_download_streams_binary_with_matching_headers(self) -> None:
        release = self._write_release(version="0.9.1", payload=b"firmware-bytes-091")
        status, raw, headers = self._download(
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(raw, release["payload"])
        self.assertEqual(headers["Content-Length"], str(len(release["payload"])))
        self.assertEqual(headers["X-Firmware-Version"], "0.9.1")
        self.assertEqual(headers["X-Firmware-SHA256"], release["sha256"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), release["sha256"])

    def test_download_rejects_invalid_device_token(self) -> None:
        self._write_release(version="0.9.1")
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": "wrong",
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 401)
        self.assertIn("令牌", response["error"])

    def test_download_returns_404_when_no_release_is_published(self) -> None:
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)
        self.assertIn("固件", response["error"])

    def test_download_refuses_to_downgrade(self) -> None:
        self._write_release(version="0.9.0", payload=b"older")
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.1",
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("最新", response["error"])

    def test_download_rejects_manifest_with_hash_mismatch(self) -> None:
        self._write_release(
            version="0.9.1",
            payload=b"real-bytes",
            manifest_sha256="0" * 64,
        )
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)
        self.assertIn("固件", response["error"])

    def test_download_rejects_manifest_with_size_mismatch(self) -> None:
        self._write_release(
            version="0.9.1",
            payload=b"twelve-bytes",
            manifest_size=99,
        )
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)

    def test_download_rejects_oversized_release(self) -> None:
        self._write_release(
            version="0.9.1",
            payload=b"small",
            manifest_size=living_margins_web.MAX_OTA_BINARY_SIZE + 1,
        )
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)

    def test_download_rejects_missing_binary(self) -> None:
        self._write_release(version="0.9.1", write_binary=False)
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)

    def test_download_rejects_manifest_with_path_escape(self) -> None:
        self._write_release(version="0.9.1", manifest_binary="../escape.bin")
        status, response, _ = self._post(
            "/api/device/firmware/download",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 404)

    def test_check_reports_no_release_when_manifest_is_missing(self) -> None:
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "0.9.0",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(response["available"])
        self.assertIsNone(response["release"])

    def test_check_rejects_invalid_current_version(self) -> None:
        self._write_release(version="0.9.1")
        status, response, _ = self._post(
            "/api/device/firmware/check",
            {
                "machine_code": DEMO_DEVICE_CODE,
                "device_token": self.device_token,
                "current_version": "not-a-version",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("版本", response["error"])



if __name__ == "__main__":
    unittest.main()
