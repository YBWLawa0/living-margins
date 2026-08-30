from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReadingHomeLayoutTests(unittest.TestCase):
    def test_reading_subject_follows_required_information_order(self) -> None:
        html = (ROOT / "web" / "src" / "App.vue").read_text(encoding="utf-8")
        home = html[html.index("view==='home'"):html.index("view==='profile'")]
        labels = ["当前书目", "识别状态", "当前页码", "随机批注", "赞同", "不赞同", "加入灵感集"]
        positions = [home.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(home.index("加入灵感集"), home.index("页边屏幕"))
        self.assertNotIn("<h1>阅读数据</h1>", home)
        self.assertNotIn('class="data-hero"', home)

    def test_reading_subject_uses_live_state_and_generous_spacing(self) -> None:
        script = (ROOT / "web" / "src" / "App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")
        for binding in (
            "snapshot?.title",
            "recognitionText",
            "pageText",
            "currentComment?.text",
            "sendFeedback('agree')",
            "sendFeedback('disagree')",
            "markInspiration",
        ):
            self.assertIn(binding, script)
        self.assertIn("const snapshot = computed", script)
        self.assertIn(".comment-preview", styles)
        self.assertIn("margin-bottom:96px", styles)
        self.assertIn(".list-section", styles)
        self.assertIn("margin:80px 0", styles)


if __name__ == "__main__":
    unittest.main()
