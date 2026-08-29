from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cloud_vision_worker import RelayPublisher, frame_channel, newest_frame, newest_frames


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

    def test_frames_are_grouped_by_user_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "user-1-session-10.jpg"
            second = root / "user-2-session-20.jpg"
            legacy = root / "user-3.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            legacy.write_bytes(b"legacy")
            self.assertEqual(frame_channel(first), (1, 10))
            self.assertIsNone(frame_channel(legacy))
            self.assertEqual(newest_frames(root), {(1, 10): first, (2, 20): second})

    def test_publisher_records_channel_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-42.json"
            publisher = RelayPublisher(path, user_id=7, session_id=42)
            publisher.publish(
                book=_Book(),
                pages=(12, 13),
                status="stable",
                event="isolated",
                comment_store=_Comments(),
            )
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["user_id"], 7)
            self.assertEqual(state["reading_session_id"], 42)

if __name__ == "__main__":
    unittest.main()
