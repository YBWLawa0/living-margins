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


class CloudRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state_path = root / "relay_state.json"
        self.path_patcher = patch.object(
            living_margins_web, "RELAY_STATE_PATH", self.state_path
        )
        self.token_patcher = patch.object(
            living_margins_web, "RELAY_TOKEN", "relay-test-secret"
        )
        self.path_patcher.start()
        self.token_patcher.start()
        database = WebDatabase(root / "app.db")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), living_margins_web.create_handler(database))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.token_patcher.stop()
        self.path_patcher.stop()
        self.temporary.cleanup()

    def post(self, token: str, state: dict[str, object]) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(
            "POST",
            "/api/relay/state",
            body=json.dumps(state).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Relay-Token": token},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    def test_authenticated_relay_publishes_state(self) -> None:
        state = {"revision": 8, "status": "stable", "pages": [40, 41]}
        status, response = self.post("relay-test-secret", state)
        self.assertEqual(status, 200)
        self.assertEqual(response["revision"], 8)
        self.assertEqual(living_margins_web.read_relay_state(), state)

    def test_relay_rejects_wrong_token_and_older_revision(self) -> None:
        status, _ = self.post("wrong", {"revision": 8, "status": "stable"})
        self.assertEqual(status, 401)
        status, _ = self.post("relay-test-secret", {"revision": 8, "status": "stable"})
        self.assertEqual(status, 200)
        status, response = self.post(
            "relay-test-secret", {"revision": 7, "status": "stable"}
        )
        self.assertEqual(status, 400)
        self.assertIn("更新", response["error"])

    def test_relay_mode_reads_pushed_state(self) -> None:
        state = {"revision": 12, "status": "turning", "pages": [40, 41]}
        living_margins_web.write_relay_state(state)
        with patch.object(living_margins_web, "VISION_SOURCE", "relay"):
            self.assertEqual(living_margins_web.current_vision_state(), state)


if __name__ == "__main__":
    unittest.main()
