from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class SessionRecorder:
    """Write one append-only event stream and a live session summary."""

    def __init__(self, debug_root: Path, config: dict, camera_info: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.session_dir = debug_root / "sessions" / stamp
        suffix = 1
        while self.session_dir.exists():
            self.session_dir = debug_root / "sessions" / f"{stamp}-{suffix}"
            suffix += 1
        self.frames_dir = self.session_dir / "frames"
        self.frames_dir.mkdir(parents=True)
        self.events_path = self.session_dir / "events.jsonl"
        self.summary_path = self.session_dir / "summary.json"
        self.started_wall = datetime.now().astimezone()
        self.started_monotonic = time.monotonic()
        self.camera_info = camera_info
        self.config = config
        self.event_sequence = 0
        self.frame_sequence = 0
        self.counters = {
            "ocr_scans": 0,
            "ocr_observations": 0,
            "ocr_no_result": 0,
            "ocr_errors": 0,
            "stale_results": 0,
            "page_confirmations": 0,
            "pending_candidates": 0,
            "manual_corrections": 0,
            "scene_changes": 0,
            "labels_correct": 0,
            "labels_incorrect": 0,
            "labels_missed": 0,
            "cover_scans": 0,
            "cover_matches": 0,
            "book_confirmations": 0,
        }
        self.ocr_durations_ms: list[float] = []
        self.confirmation_delays_s: list[float] = []
        self.record_event("session_started", camera=camera_info, config=config)

    def record_event(self, event_type: str, **details: Any) -> int:
        self.event_sequence += 1
        record = {
            "sequence": self.event_sequence,
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "elapsed_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "event": event_type,
            **_json_value(details),
        }
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.write_summary()
        return self.event_sequence

    def save_keyframe(self, frame, kind: str) -> str | None:
        self.frame_sequence += 1
        filename = f"{self.frame_sequence:04d}-{kind}.jpg"
        path = self.frames_dir / filename
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            return None
        return str(path.relative_to(self.session_dir)).replace("\\", "/")

    def record_scene_change(self, confirmed) -> None:
        self.counters["scene_changes"] += 1
        self.record_event("scene_changed", confirmed=confirmed)

    def record_ocr(
        self,
        *,
        duration_ms: float,
        mode: str,
        reanchor: bool,
        generation: int,
        observation,
        candidates: list[dict],
        ranked: list[dict],
        decision: str,
        consensus_before: dict,
        consensus_after: dict,
        state: str,
        page_box=None,
        keyframe: str | None = None,
        error: str | None = None,
    ) -> int:
        self.counters["ocr_scans"] += 1
        self.ocr_durations_ms.append(duration_ms)
        if error:
            self.counters["ocr_errors"] += 1
        elif observation is None:
            self.counters["ocr_no_result"] += 1
        else:
            self.counters["ocr_observations"] += 1
        if decision == "stale":
            self.counters["stale_results"] += 1
        elif decision.startswith("pending"):
            self.counters["pending_candidates"] += 1
        elif decision == "confirmed":
            self.counters["page_confirmations"] += 1
        return self.record_event(
            "ocr_completed",
            duration_ms=round(duration_ms, 1),
            mode=mode,
            reanchor=reanchor,
            generation=generation,
            observation=observation,
            candidates=candidates,
            ranked=ranked,
            decision=decision,
            consensus_before=consensus_before,
            consensus_after=consensus_after,
            state=state,
            page_box=page_box,
            keyframe=keyframe,
            error=error,
        )

    def record_confirmation_delay(self, seconds: float) -> None:
        self.confirmation_delays_s.append(seconds)
        self.write_summary()

    def record_cover(
        self,
        *,
        duration_ms: float,
        generation: int,
        decision: str,
        match: dict | None,
        pending_book: str | None,
        pending_count: int,
        required_count: int,
        keyframe: str | None = None,
        error: str | None = None,
    ) -> int:
        self.counters["cover_scans"] += 1
        if match is not None:
            self.counters["cover_matches"] += 1
        if decision == "confirmed":
            self.counters["book_confirmations"] += 1
        return self.record_event(
            "cover_match_completed",
            duration_ms=round(duration_ms, 1),
            generation=generation,
            decision=decision,
            match=match,
            pending_book=pending_book,
            pending_count=pending_count,
            required_count=required_count,
            keyframe=keyframe,
            error=error,
        )

    def record_label(self, label: str, confirmed, confirmation_event: int | None, keyframe: str | None) -> None:
        counter = {
            "correct": "labels_correct",
            "incorrect": "labels_incorrect",
            "missed": "labels_missed",
        }[label]
        self.counters[counter] += 1
        self.record_event(
            "test_label",
            label=label,
            confirmed=confirmed,
            confirmation_event=confirmation_event,
            keyframe=keyframe,
        )

    def record_manual_correction(self, direction: str, confirmed) -> None:
        self.counters["manual_corrections"] += 1
        self.record_event("manual_correction", direction=direction, confirmed=confirmed)

    def summary(self) -> dict:
        correct = self.counters["labels_correct"]
        incorrect = self.counters["labels_incorrect"]
        labeled = correct + incorrect
        labeled_pages = labeled + self.counters["labels_missed"]
        scans = self.counters["ocr_scans"]
        durations = self.ocr_durations_ms
        delays = self.confirmation_delays_s
        return {
            "session_started": self.started_wall.isoformat(timespec="seconds"),
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_seconds": round(time.monotonic() - self.started_monotonic, 1),
            "camera": self.camera_info,
            "counters": dict(self.counters),
            "ocr_duration_ms": {
                "average": round(sum(durations) / len(durations), 1) if durations else None,
                "minimum": round(min(durations), 1) if durations else None,
                "maximum": round(max(durations), 1) if durations else None,
            },
            "confirmation_delay_seconds": {
                "average": round(sum(delays) / len(delays), 2) if delays else None,
                "maximum": round(max(delays), 2) if delays else None,
                "samples": len(delays),
            },
            "labeled_confirmation_accuracy": round(correct / labeled, 4) if labeled else None,
            "labeled_page_success_rate": round(correct / labeled_pages, 4) if labeled_pages else None,
            "ocr_observation_rate": (
                round(self.counters["ocr_observations"] / scans, 4) if scans else None
            ),
            "ocr_no_result_rate": round(self.counters["ocr_no_result"] / scans, 4) if scans else None,
            "labels_note": "Press Y=correct, N=incorrect, M=missed during a test to measure accuracy.",
        }

    def write_summary(self) -> None:
        temporary = self.summary_path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.summary(), handle, ensure_ascii=False, indent=2)
        temporary.replace(self.summary_path)

    def close(self) -> None:
        self.record_event("session_ended", summary=self.summary())
