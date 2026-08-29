from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cloud_vision_worker import RelayPublisher, newest_frame


class _Book:
    book_id = "book-1"
    title = "测试书籍"


class _Comments:
    def select(self, book_id, pages):
        return None


class CloudVisionWorkerTests(unittest.TestCase):
    def test_publisher_is_atomic_versioned_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.json"
            publisher = RelayPublisher(path)
            self.assertTrue(publisher.publish(book=_Book(), pages=(10, 11), status="stable", event="test", comment_store=_Comments()))
            first = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(first["revision"], 1)
            self.assertEqual(first["pages"], [10, 11])
            self.assertFalse(publisher.publish(book=_Book(), pages=(10, 11), status="stable", event="again", comment_store=_Comments()))

    def test_newest_frame_selects_latest_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "user-1.jpg"
            second = root / "user-2.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            first.touch()
            second.touch()
            self.assertEqual(newest_frame(root), second)


if __name__ == "__main__":
    unittest.main()
