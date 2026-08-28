from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from library_terra.comments import CommentStore


class CommentStoreTests(unittest.TestCase):
    def make_store(self, root: Path) -> CommentStore:
        book_dir = root / "terra-demo"
        book_dir.mkdir(parents=True)
        (book_dir / "book.json").write_text(
            json.dumps({"id": "terra-demo", "title": "Terra Demo"}),
            encoding="utf-8",
        )
        return CommentStore(root)

    def test_add_update_delete_and_select_for_spread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            left = store.upsert("terra-demo", page=66, text="左页批注", author="Alice")
            right = store.upsert("terra-demo", page=67, text="右页高优先级批注", author="Bob", priority=2)

            selected = store.select("terra-demo", (66, 67))
            self.assertEqual(right.comment_id, selected.comment_id if selected else None)
            self.assertEqual("右页高优先级批注", selected.text if selected else None)

            updated = store.upsert(
                "terra-demo",
                page=67,
                text="修改后的批注",
                author="Bob",
                priority=2,
                comment_id=right.comment_id,
            )
            self.assertEqual(right.comment_id, updated.comment_id)
            self.assertEqual(2, len(store.list("terra-demo")))
            self.assertEqual("修改后的批注", store.select("terra-demo", (66, 67)).text)

            self.assertTrue(store.delete("terra-demo", right.comment_id))
            self.assertEqual(left.comment_id, store.select("terra-demo", (66, 67)).comment_id)
            self.assertFalse(store.delete("terra-demo", "missing"))

    def test_disabled_and_malformed_comments_are_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            path = root / "terra-demo" / "comments.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "comments": [
                            {"id": "disabled", "page": 20, "text": "隐藏", "enabled": False},
                            {"id": "bad", "page": "nope", "text": "错误"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertIsNone(store.select("terra-demo", (20, 21)))

    def test_range_comment_matches_either_page_and_keeps_range_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            comment = store.upsert(
                "terra-demo",
                page=66,
                page_end=67,
                text="跨页上下文批注",
                author="Reader",
                comment_id="web-comment-1",
            )

            self.assertEqual(store.select("terra-demo", (65, 66)), comment)
            self.assertEqual(store.select("terra-demo", (67, 68)), comment)
            self.assertEqual(comment.state_value()["page_end"], 67)


if __name__ == "__main__":
    unittest.main()
