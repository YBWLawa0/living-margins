from __future__ import annotations

import unittest
from datetime import datetime

import cv2
import numpy as np

from library_terra.enrollment import (
    extract_ocr_lines,
    find_cover_quad,
    make_book_id,
    suggest_title,
    warp_cover,
)


def ocr_item(text: str, confidence: float, box: tuple[int, int, int, int]):
    x1, y1, x2, y2 = box
    polygon = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    return polygon, text, confidence


class EnrollmentTests(unittest.TestCase):
    def test_finds_and_rectifies_cover_quad(self) -> None:
        frame = np.full((720, 960, 3), 25, dtype=np.uint8)
        polygon = np.array([[260, 65], [700, 110], [650, 660], [180, 610]], dtype=np.int32)
        cv2.fillConvexPoly(frame, polygon, (235, 235, 235))
        cv2.polylines(frame, [polygon], True, (255, 255, 255), 8)
        cv2.putText(frame, "TERRA", (300, 300), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (10, 10, 10), 5)

        quad = find_cover_quad(frame)
        self.assertIsNotNone(quad)
        rectified = warp_cover(frame, quad)
        self.assertGreater(rectified.shape[0], rectified.shape[1])
        self.assertGreater(rectified.shape[0], 450)

    def test_large_cover_text_becomes_title_suggestion(self) -> None:
        result = [
            ocr_item("Library", 0.96, (80, 100, 320, 170)),
            ocr_item("Terra", 0.94, (100, 180, 300, 250)),
            ocr_item("An Author", 0.99, (130, 470, 270, 492)),
            ocr_item("Publisher", 0.98, (150, 555, 250, 570)),
        ]
        lines = extract_ocr_lines(result, (600, 400, 3))

        self.assertEqual("Library Terra", suggest_title(lines, (600, 400, 3)))

    def test_book_id_is_automatic_for_ascii_and_non_ascii_titles(self) -> None:
        now = datetime(2026, 8, 26, 5, 30, 15)
        self.assertEqual("library-terra-053015", make_book_id("Library Terra", now))
        self.assertEqual("book-20260826-053015", make_book_id("大家的日本語", now))


if __name__ == "__main__":
    unittest.main()
