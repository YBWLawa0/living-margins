from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library_terra.web_database import DEMO_DEVICE_CODE, WebDatabase


class WebDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = WebDatabase(Path(self.temporary.name) / "living_margins.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_user_login_token_and_logout(self) -> None:
        user = self.database.create_user("reader", "secret12")
        self.assertEqual(user["role"], "admin")
        self.assertIsNone(self.database.authenticate("reader", "wrong-password"))
        self.assertEqual(self.database.authenticate("READER", "secret12"), user)

        token = self.database.create_auth_session(user["id"])
        self.assertEqual(self.database.user_for_token(token), user)
        self.database.revoke_token(token)
        self.assertIsNone(self.database.user_for_token(token))

    def test_first_user_is_demo_admin_and_later_users_are_readers(self) -> None:
        first = self.database.create_user("first", "secret12")
        second = self.database.create_user("second", "secret12")
        self.assertEqual(first["role"], "admin")
        self.assertEqual(second["role"], "reader")

    def test_duplicate_username_is_rejected_case_insensitively(self) -> None:
        self.database.create_user("PaperMoon", "secret12")
        with self.assertRaisesRegex(ValueError, "已经存在"):
            self.database.create_user("papermoon", "another-secret")

    def test_device_binding_persists_and_is_exclusive(self) -> None:
        first = self.database.create_user("first", "secret12")
        second = self.database.create_user("second", "secret12")
        device = self.database.bind_device(first["id"], DEMO_DEVICE_CODE.lower())

        self.assertEqual(self.database.devices_for_user(first["id"]), [device])
        with self.assertRaisesRegex(ValueError, "其他用户"):
            self.database.bind_device(second["id"], DEMO_DEVICE_CODE)

    def test_device_feedback_is_unique_per_user_and_supports_vote_change(self) -> None:
        user = self.database.create_user("reader", "secret12")
        with self.assertRaisesRegex(ValueError, "尚未绑定"):
            self.database.feedback_for_device(DEMO_DEVICE_CODE, "comment-1")
        self.database.bind_device(user["id"], DEMO_DEVICE_CODE)

        created, created_outcome = self.database.submit_device_feedback(
            DEMO_DEVICE_CODE,
            "comment-1",
            "agree",
            book_id="book-a",
            page=66,
        )
        unchanged, unchanged_outcome = self.database.submit_device_feedback(
            DEMO_DEVICE_CODE,
            "comment-1",
            "agree",
            book_id="book-a",
            page=66,
        )
        changed, changed_outcome = self.database.submit_device_feedback(
            DEMO_DEVICE_CODE,
            "comment-1",
            "disagree",
            book_id="book-a",
            page=66,
        )

        self.assertEqual(created_outcome, "created")
        self.assertEqual(unchanged_outcome, "unchanged")
        self.assertEqual(changed_outcome, "changed")
        self.assertEqual(created["id"], unchanged["id"])
        self.assertEqual(created["id"], changed["id"])
        self.assertEqual(changed["action"], "disagree")
        self.assertEqual(
            self.database.feedback_for_device(DEMO_DEVICE_CODE, "comment-1"), changed
        )

        with self.database._connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM comment_feedback WHERE comment_id = 'comment-1'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_pause_freezes_context_until_resume(self) -> None:
        user = self.database.create_user("reader", "secret12")
        device = self.database.bind_device(user["id"], DEMO_DEVICE_CODE)
        initial = {
            "book_id": "book-a",
            "title": "纸上的书",
            "pages": [66, 67],
            "revision": 3,
        }
        session = self.database.start_reading_session(user["id"], device["id"], initial)
        self.assertEqual(session["pages"], [66, 67])

        self.database.set_reading_status(user["id"], "paused")
        self.database.sync_reading_context(
            user["id"], {"book_id": "book-a", "title": "纸上的书", "pages": [68, 69], "revision": 4}
        )
        frozen = self.database.current_reading_session(user["id"])
        self.assertEqual(frozen["pages"], [66, 67])

        self.database.set_reading_status(user["id"], "active")
        self.database.sync_reading_context(
            user["id"], {"book_id": "book-a", "title": "纸上的书", "pages": [68, 69], "revision": 4}
        )
        resumed = self.database.current_reading_session(user["id"])
        self.assertEqual(resumed["pages"], [68, 69])

    def test_starting_new_session_ends_previous_one(self) -> None:
        user = self.database.create_user("reader", "secret12")
        first = self.database.start_reading_session(user["id"], None, None)
        second = self.database.start_reading_session(user["id"], None, None)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.database.current_reading_session(user["id"])["id"], second["id"])

    def test_comment_draft_freezes_context_and_submits_for_review(self) -> None:
        user = self.database.create_user("annotator", "secret12")
        session = self.database.start_reading_session(
            user["id"],
            None,
            {
                "book_id": "book-a",
                "title": "纸上的书",
                "pages": [66, 67],
                "revision": 9,
            },
        )
        self.database.set_reading_status(user["id"], "paused")

        draft = self.database.save_comment_draft(user["id"], "第一版")
        self.assertEqual(draft["reading_session_id"], session["id"])
        self.assertEqual(draft["book_id"], "book-a")
        self.assertEqual(draft["pages"], [66, 67])
        self.assertEqual(draft["status"], "draft")

        updated = self.database.save_comment_draft(
            user["id"], "修改后的想法", draft["id"]
        )
        self.assertEqual(updated["id"], draft["id"])
        self.assertEqual(updated["body"], "修改后的想法")

        submitted = self.database.submit_comment(user["id"], draft["id"])
        self.assertEqual(submitted["status"], "pending")
        self.assertIsNotNone(submitted["submitted_at"])
        self.assertEqual(self.database.comments_for_user(user["id"]), [submitted])

        with self.assertRaisesRegex(ValueError, "已经提交"):
            self.database.submit_comment(user["id"], draft["id"])
        with self.assertRaisesRegex(ValueError, "只有草稿"):
            self.database.save_comment_draft(user["id"], "不能再改", draft["id"])

    def test_new_comment_requires_paused_trusted_context(self) -> None:
        user = self.database.create_user("annotator", "secret12")
        self.database.start_reading_session(user["id"], None, None)
        with self.assertRaisesRegex(ValueError, "先暂停"):
            self.database.save_comment_draft(user["id"], "想法")

        self.database.set_reading_status(user["id"], "paused")
        with self.assertRaisesRegex(ValueError, "没有识别到书籍"):
            self.database.save_comment_draft(user["id"], "想法")

    def test_comment_ownership_is_enforced(self) -> None:
        owner = self.database.create_user("owner", "secret12")
        stranger = self.database.create_user("stranger", "secret12")
        self.database.start_reading_session(
            owner["id"],
            None,
            {"book_id": "book-a", "title": "纸上的书", "pages": [20, 21], "revision": 1},
        )
        self.database.set_reading_status(owner["id"], "paused")
        draft = self.database.save_comment_draft(owner["id"], "只属于作者")

        with self.assertRaisesRegex(ValueError, "没有找到"):
            self.database.save_comment_draft(stranger["id"], "越权修改", draft["id"])
        with self.assertRaisesRegex(ValueError, "没有找到"):
            self.database.submit_comment(stranger["id"], draft["id"])

    def test_admin_can_review_pending_comment_and_reader_cannot(self) -> None:
        admin = self.database.create_user("admin", "secret12")
        reader = self.database.create_user("reader", "secret12")
        self.database.start_reading_session(
            reader["id"],
            None,
            {"book_id": "book-a", "title": "纸上的书", "pages": [66, 67], "revision": 1},
        )
        self.database.set_reading_status(reader["id"], "paused")
        draft = self.database.save_comment_draft(reader["id"], "来自读者的批注")
        submitted = self.database.submit_comment(reader["id"], draft["id"])

        with self.assertRaisesRegex(ValueError, "只有管理员"):
            self.database.pending_comments_for_admin(reader["id"])
        with self.assertRaisesRegex(ValueError, "只有管理员"):
            self.database.review_comment(reader["id"], submitted["id"], "approved")

        queue = self.database.pending_comments_for_admin(admin["id"])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["author_username"], "reader")
        reviewed = self.database.review_comment(admin["id"], submitted["id"], "approved")
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(self.database.pending_comments_for_admin(admin["id"]), [])

    def test_inspiration_marks_without_pausing_and_converts_to_frozen_draft(self) -> None:
        user = self.database.create_user("reader", "secret12")
        session = self.database.start_reading_session(
            user["id"],
            None,
            {"book_id": "book-a", "title": "纸上的书", "pages": [66, 67], "revision": 5},
        )
        vision = {
            "book_id": "book-a",
            "title": "纸上的书",
            "pages": [66, 67],
            "revision": 8,
        }
        inspiration, created = self.database.mark_inspiration(user["id"], vision)
        duplicate, duplicate_created = self.database.mark_inspiration(user["id"], vision)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["id"], inspiration["id"])
        self.assertEqual(inspiration["reading_session_id"], session["id"])
        self.assertEqual(self.database.current_reading_session(user["id"])["status"], "active")
        self.assertEqual(self.database.inspirations_for_user(user["id"]), [inspiration])

        converted, draft = self.database.convert_inspiration_to_draft(
            user["id"], inspiration["id"]
        )
        self.assertEqual(converted["status"], "converted")
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["pages"], [66, 67])
        self.assertEqual(draft["inspiration_id"], inspiration["id"])
        self.assertEqual(self.database.inspirations_for_user(user["id"]), [])

        updated = self.database.save_comment_draft(user["id"], "后来补写的想法", draft["id"])
        self.assertEqual(updated["body"], "后来补写的想法")

    def test_inspiration_conversion_enforces_ownership(self) -> None:
        owner = self.database.create_user("owner", "secret12")
        stranger = self.database.create_user("stranger", "secret12")
        self.database.start_reading_session(owner["id"], None, None)
        inspiration, _ = self.database.mark_inspiration(
            owner["id"],
            {"book_id": "book-a", "title": "纸上的书", "pages": [20, 21]},
        )
        with self.assertRaisesRegex(ValueError, "没有找到"):
            self.database.convert_inspiration_to_draft(stranger["id"], inspiration["id"])


if __name__ == "__main__":
    unittest.main()
