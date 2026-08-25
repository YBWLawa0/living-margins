from __future__ import annotations

import unittest

from library_terra.vision import Candidate, choose_page_spread


class PageRankingTests(unittest.TestCase):
    def test_visually_strong_large_jump_reaches_consensus_layer(self) -> None:
        # Geometry and OCR confidence mirror the real 110-111 session frame.
        candidate = Candidate(
            value=111,
            score=0.83,
            box=(888, 512, 995, 531),
            raw="3...111",
            token_index=2,
            token_count=3,
        )

        spread, ranked = choose_page_spread(
            [candidate],
            (720, 1280, 3),
            previous=(90, 91),
            max_jump=12,
            reanchor=False,
            positions=["bottom_outer"],
        )

        self.assertEqual((110, 111), spread)
        self.assertGreaterEqual(ranked[0].rank, 3.75)

    def test_active_page_number_beats_exposed_underlying_sheet(self) -> None:
        candidates = [
            Candidate(32, 0.996, (163, 462, 185, 483), "32"),
            Candidate(16, 0.998, (99, 480, 118, 498), "16"),
        ]

        spread, ranked = choose_page_spread(
            candidates,
            (720, 1280, 3),
            previous=(74, 75),
            max_jump=12,
            reanchor=True,
            positions=["bottom_outer"],
            page_box=(0, 0, 1280, 588),
        )

        self.assertEqual((32, 33), spread)
        self.assertEqual(32, ranked[0].candidate.value)
        self.assertGreater(ranked[0].rank - ranked[1].rank, 0.30)


if __name__ == "__main__":
    unittest.main()
