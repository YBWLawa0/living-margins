from __future__ import annotations

import unittest

from library_terra.vision import MotionGate


class MotionGateTests(unittest.TestCase):
    def make_gate(self) -> MotionGate:
        gate = MotionGate(settle_seconds=0.8, start_frames=3, window_frames=5)
        gate.last_activity = 0.0
        return gate

    def test_single_noise_spike_does_not_start_scene_change(self) -> None:
        gate = self.make_gate()
        starts = [gate.update(value, index * 0.1)[0] for index, value in enumerate([False, True, False, False])]
        self.assertFalse(any(starts))

    def test_three_of_five_motion_frames_start_one_episode(self) -> None:
        gate = self.make_gate()
        samples = [True, False, True, False, True, True, False]
        starts = [gate.update(value, index * 0.1)[0] for index, value in enumerate(samples)]
        self.assertEqual(1, sum(starts))
        self.assertTrue(gate.active)

    def test_episode_ends_only_after_quiet_period(self) -> None:
        gate = self.make_gate()
        gate.update(True, 0.0)
        gate.update(True, 0.1)
        started, _ = gate.update(True, 0.2)
        self.assertTrue(started)

        self.assertEqual((False, False), gate.update(False, 0.7))
        self.assertEqual((False, False), gate.update(False, 0.99))
        self.assertEqual((False, True), gate.update(False, 1.01))
        self.assertTrue(gate.is_settled(1.01))


if __name__ == "__main__":
    unittest.main()
