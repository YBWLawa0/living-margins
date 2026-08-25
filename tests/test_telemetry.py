from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from library_terra.telemetry import SessionRecorder


class SessionRecorderTests(unittest.TestCase):
    def test_writes_events_keyframes_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = SessionRecorder(Path(directory), {"debug": True}, "test camera")
            frame = np.zeros((40, 60, 3), dtype=np.uint8)
            keyframe = recorder.save_keyframe(frame, "confirmed")
            event_id = recorder.record_ocr(
                duration_ms=125.4,
                mode="continuous",
                reanchor=False,
                generation=2,
                observation=(66, 67),
                candidates=[{"value": 67}],
                ranked=[{"spread": [66, 67]}],
                decision="confirmed",
                consensus_before={"confirmed": None},
                consensus_after={"confirmed": (66, 67)},
                state="STABLE",
                page_box=(0, 0, 60, 40),
                keyframe=keyframe,
            )
            recorder.record_confirmation_delay(1.25)
            recorder.record_label("correct", (66, 67), event_id, keyframe)
            recorder.close()

            events = recorder.events_path.read_text(encoding="utf-8").splitlines()
            summary = json.loads(recorder.summary_path.read_text(encoding="utf-8"))
            self.assertEqual(4, len(events))
            self.assertTrue((recorder.session_dir / str(keyframe)).exists())
            self.assertEqual(1, summary["counters"]["ocr_scans"])
            self.assertEqual(1, summary["counters"]["page_confirmations"])
            self.assertEqual(1.0, summary["labeled_confirmation_accuracy"])
            self.assertEqual(1.0, summary["labeled_page_success_rate"])
            self.assertEqual(1.0, summary["ocr_observation_rate"])
            self.assertEqual(0.0, summary["ocr_no_result_rate"])
            self.assertEqual(1.25, summary["confirmation_delay_seconds"]["average"])


if __name__ == "__main__":
    unittest.main()
