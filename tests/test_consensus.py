from __future__ import annotations

import unittest

from app import PageConsensus


class PageConsensusTests(unittest.TestCase):
    def test_single_false_observation_does_not_replace_page(self) -> None:
        consensus = PageConsensus(confirmations=2, large_jump_confirmations=3, max_jump=12)
        consensus.set_confirmed((84, 85))

        changed, current = consensus.observe((174, 175))
        self.assertFalse(changed)
        self.assertEqual((84, 85), current)

        changed, current = consensus.observe((84, 85))
        self.assertFalse(changed)
        self.assertEqual((84, 85), current)
        self.assertIsNone(consensus.pending)

    def test_two_nearby_observations_confirm_a_page(self) -> None:
        consensus = PageConsensus(confirmations=2, large_jump_confirmations=3, max_jump=12)
        consensus.set_confirmed((84, 85))

        self.assertFalse(consensus.observe((86, 87))[0])
        changed, current = consensus.observe((86, 87))

        self.assertTrue(changed)
        self.assertEqual((86, 87), current)

    def test_large_jump_requires_three_observations(self) -> None:
        consensus = PageConsensus(confirmations=2, large_jump_confirmations=3, max_jump=12)
        consensus.set_confirmed((20, 21))

        self.assertFalse(consensus.observe((80, 81))[0])
        self.assertFalse(consensus.observe((80, 81))[0])
        changed, current = consensus.observe((80, 81))

        self.assertTrue(changed)
        self.assertEqual((80, 81), current)


if __name__ == "__main__":
    unittest.main()
