from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from library_terra.books import BookConsensus, CoverMatch, CoverMatcher


def make_cover() -> np.ndarray:
    image = np.full((600, 400, 3), 238, dtype=np.uint8)
    cv2.rectangle(image, (18, 18), (382, 582), (20, 40, 120), 10)
    cv2.putText(image, "LIBRARY", (48, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.45, (10, 10, 10), 4)
    cv2.putText(image, "TERRA", (78, 185), cv2.FONT_HERSHEY_SIMPLEX, 1.75, (20, 80, 160), 5)
    cv2.circle(image, (200, 330), 105, (30, 130, 60), 9)
    cv2.line(image, (95, 410), (305, 250), (120, 30, 30), 8)
    rng = np.random.default_rng(7)
    for x, y in rng.integers([35, 220], [365, 555], size=(80, 2)):
        cv2.circle(image, (int(x), int(y)), 2, (0, 0, 0), -1)
    return image


class CoverMatcherTests(unittest.TestCase):
    def test_matches_rotated_perspective_cover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book_dir = root / "terra-demo"
            book_dir.mkdir()
            cover = make_cover()
            cv2.imwrite(str(book_dir / "cover.png"), cover)
            (book_dir / "book.json").write_text(
                json.dumps({"id": "terra-demo", "title": "Terra Demo", "cover": "cover.png"}),
                encoding="utf-8",
            )

            frame = np.full((720, 960, 3), 45, dtype=np.uint8)
            source = np.float32([[0, 0], [399, 0], [399, 599], [0, 599]])
            target = np.float32([[250, 75], [690, 120], [650, 650], [180, 610]])
            homography = cv2.getPerspectiveTransform(source, target)
            warped = cv2.warpPerspective(cover, homography, (960, 720))
            mask = cv2.warpPerspective(np.full((600, 400), 255, dtype=np.uint8), homography, (960, 720))
            frame[mask > 0] = warped[mask > 0]

            matcher = CoverMatcher(root, {})
            match = matcher.match(frame)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual("terra-demo", match.book.book_id)
            self.assertGreaterEqual(match.inliers, matcher.min_inliers)

    def test_plain_page_does_not_match_cover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book_dir = root / "terra-demo"
            book_dir.mkdir()
            cv2.imwrite(str(book_dir / "cover.png"), make_cover())
            (book_dir / "book.json").write_text(
                json.dumps({"id": "terra-demo", "title": "Terra Demo", "cover": "cover.png"}),
                encoding="utf-8",
            )
            page = np.full((720, 960, 3), 245, dtype=np.uint8)
            for row in range(80, 640, 35):
                cv2.line(page, (130, row), (830, row), (80, 80, 80), 2)

            self.assertIsNone(CoverMatcher(root, {}).match(page))

    def test_book_consensus_requires_two_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book_dir = root / "terra-demo"
            book_dir.mkdir()
            cv2.imwrite(str(book_dir / "cover.png"), make_cover())
            (book_dir / "book.json").write_text(
                json.dumps({"id": "terra-demo", "title": "Terra Demo", "cover": "cover.png"}),
                encoding="utf-8",
            )
            entry = CoverMatcher(root, {}).entries[0]
            match = CoverMatch(entry, 30.0, 25, 18, 0.72, 0.4)
            consensus = BookConsensus(2)

            self.assertFalse(consensus.observe(match)[0])
            self.assertFalse(consensus.observe(None)[0])
            self.assertIsNone(consensus.pending)
            self.assertFalse(consensus.observe(match)[0])
            changed, confirmed = consensus.observe(match)
            self.assertTrue(changed)
            self.assertEqual("terra-demo", confirmed.book_id if confirmed else None)


if __name__ == "__main__":
    unittest.main()
