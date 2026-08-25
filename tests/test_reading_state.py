from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from library_terra.reading_state import ReadingStatePublisher, StateHttpServer


class ReadingStatePublisherTests(unittest.TestCase):
    def test_publishes_atomic_snapshot_deduplicates_and_keeps_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publisher = ReadingStatePublisher(root)
            changed, first = publisher.publish(
                book_id="terra-demo",
                title="Terra Demo",
                pages=None,
                status="book_confirmed",
                source="cover_match",
                event="book_confirmed",
            )
            duplicate_changed, duplicate = publisher.publish(
                book_id="terra-demo",
                title="Terra Demo",
                pages=None,
                status="book_confirmed",
                source="cover_match",
                event="ignored_duplicate",
            )
            page_changed, page = publisher.publish(
                book_id="terra-demo",
                title="Terra Demo",
                pages=(66, 67),
                status="stable",
                source="page_ocr",
                event="page_confirmed",
                comment={"id": "comment-1", "page": 66, "text": "测试批注", "author": "读者"},
            )

            self.assertTrue(changed)
            self.assertFalse(duplicate_changed)
            self.assertTrue(page_changed)
            self.assertEqual(first, duplicate)
            self.assertEqual(first["revision"] + 1, page["revision"])
            self.assertEqual([66, 67], page["pages"])
            same_changed, same_state = publisher.publish(
                book_id="terra-demo",
                title="Terra Demo",
                pages=(66, 67),
                status="stable",
                source="comment_store",
                event="comment_refreshed",
                comment={"id": "comment-1", "page": 66, "text": "测试批注", "author": "读者"},
            )
            self.assertFalse(same_changed)
            self.assertEqual(page, same_state)
            self.assertEqual(page, json.loads((root / "state.json").read_text(encoding="utf-8")))
            self.assertEqual(2, len((root / "events.jsonl").read_text(encoding="utf-8").splitlines()))

            restarted = ReadingStatePublisher(root)
            _, after_restart = restarted.publish(
                book_id=None,
                title=None,
                pages=None,
                status="searching_book",
                source="system",
                event="observer_started",
            )
            self.assertEqual(page["revision"] + 1, after_restart["revision"])

    def test_http_state_and_feedback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = ReadingStatePublisher(Path(directory))
            publisher.publish(
                book_id="terra-demo",
                title="Terra Demo",
                pages=(74, 75),
                status="stable",
                source="page_ocr",
                event="page_confirmed",
                comment={"id": "comment-1", "page": 74, "text": "测试批注", "author": "读者"},
            )
            server = StateHttpServer(publisher, "127.0.0.1", 0)
            server.start()
            try:
                _, port = server.address
                with urlopen(f"http://127.0.0.1:{port}/state", timeout=2) as response:
                    state = json.loads(response.read().decode("utf-8"))
                payload = json.dumps({"action": "agree", "device_id": "virtual-screen"}).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{port}/feedback",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    feedback_response = json.loads(response.read().decode("utf-8"))
            finally:
                server.close()

            feedback = json.loads(publisher.feedback_path.read_text(encoding="utf-8").strip())
            self.assertEqual([74, 75], state["pages"])
            self.assertTrue(feedback_response["ok"])
            self.assertEqual("agree", feedback["action"])
            self.assertEqual(state["revision"], feedback["state_revision"])
            self.assertEqual("comment-1", feedback["comment_id"])


if __name__ == "__main__":
    unittest.main()
