from __future__ import annotations

import argparse
import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path

import cv2
from rapidocr_onnxruntime import RapidOCR

from library_terra.books import BookConsensus, CoverMatcher
from library_terra.comments import CommentStore
from library_terra.reading_state import ReadingStatePublisher, StateHttpServer
from library_terra.telemetry import SessionRecorder
from library_terra.vision import (
    Candidate,
    MotionDetector,
    MotionGate,
    RankedCandidate,
    choose_page_spread,
    detect_book_box,
    ocr_candidates,
    sharpness,
)

ROOT = Path(__file__).resolve().parent
WINDOW_NAME = "Library Terra - Book and Page Observer V5"


class ScanState(str, Enum):
    STABLE = "STABLE"
    TURNING = "TURNING"
    RECOGNIZING = "RECOGNIZING"
    VERIFYING = "VERIFYING"
    UNCONFIRMED = "UNCONFIRMED"


class PageConsensus:
    """Debounce OCR observations so one bad frame never changes the page."""

    def __init__(self, confirmations: int, large_jump_confirmations: int, max_jump: int):
        self.confirmations = max(2, confirmations)
        self.large_jump_confirmations = max(self.confirmations, large_jump_confirmations)
        self.max_jump = max_jump
        self.confirmed: tuple[int, int] | None = None
        self.pending: tuple[int, int] | None = None
        self.pending_count = 0
        self.required_count = self.confirmations

    def reset(self) -> None:
        self.confirmed = None
        self.clear_pending()

    def clear_pending(self) -> None:
        self.pending = None
        self.pending_count = 0
        self.required_count = self.confirmations

    def scene_changed(self) -> None:
        self.clear_pending()

    def set_confirmed(self, pair: tuple[int, int] | None) -> None:
        self.confirmed = pair
        self.clear_pending()

    def snapshot(self) -> dict:
        return {
            "confirmed": self.confirmed,
            "pending": self.pending,
            "pending_count": self.pending_count,
            "required_count": self.required_count,
        }

    def observe(self, pair: tuple[int, int] | None) -> tuple[bool, tuple[int, int] | None]:
        if pair is None:
            return False, self.confirmed
        if pair == self.confirmed:
            self.clear_pending()
            return False, self.confirmed
        if pair == self.pending:
            self.pending_count += 1
        else:
            self.pending = pair
            self.pending_count = 1

        jump = abs(pair[0] - self.confirmed[0]) if self.confirmed else 0
        self.required_count = self.large_jump_confirmations if self.confirmed and jump > self.max_jump else self.confirmations
        if self.pending_count < self.required_count:
            return False, self.confirmed
        self.confirmed = pair
        self.clear_pending()
        return True, self.confirmed


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def publish_reading_state(
    publisher: ReadingStatePublisher,
    active_book,
    pages: tuple[int, int] | None,
    comment_store: CommentStore | None = None,
    *,
    status: str,
    source: str,
    event: str,
) -> None:
    comment = None
    if comment_store is not None and active_book is not None and pages is not None:
        selected = comment_store.select(active_book.book_id, pages)
        comment = selected.state_value() if selected is not None else None
    publisher.publish(
        book_id=active_book.book_id if active_book else None,
        title=active_book.title if active_book else None,
        pages=pages,
        status=status,
        source=source,
        event=event,
        comment=comment,
    )


def open_camera(index: int, cfg: dict):
    """Open a Windows camera with a modern backend and a safe fallback."""
    requested = str(cfg.get("camera_backend", "MSMF")).upper()
    options = [("MSMF", cv2.CAP_MSMF), ("DSHOW", cv2.CAP_DSHOW)]
    if requested == "DSHOW":
        options.reverse()

    errors = []
    for name, backend in options:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            errors.append(f"{name}: unavailable")
            continue

        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if name == "DSHOW" and cfg.get("use_mjpeg", True):
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["frame_width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["frame_height"])
        cap.set(cv2.CAP_PROP_FPS, cfg.get("camera_fps", 30))
        if cfg.get("autofocus", True):
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1.0 if name == "MSMF" else 0.75)

        ok, frame = False, None
        for _ in range(12):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
        if not ok:
            cap.release()
            errors.append(f"{name}: opened but returned no frames")
            continue

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        fourcc_value = int(cap.get(cv2.CAP_PROP_FOURCC))
        fourcc = "".join(chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00") or "unknown"
        info = f"{name} {width}x{height} @{fps:.0f}fps {fourcc}"
        print(f"Camera mode: {info}")
        return cap, frame, info

    raise SystemExit(f"Cannot open camera {index}. " + "; ".join(errors))


def scan_full_frame(engine: RapidOCR, frame, cfg: dict, previous, reanchor: bool):
    """Recognize the untouched frame once, then rank numeric OCR lines."""
    result, _ = engine(frame, use_det=True, use_cls=False, use_rec=True)
    candidates = ocr_candidates(result, float(cfg["min_ocr_confidence"]))
    page_box = detect_book_box(frame)
    pair, ranked = choose_page_spread(
        candidates,
        frame.shape,
        previous,
        int(cfg["max_page_jump"]),
        reanchor=reanchor,
        positions=cfg.get("page_number_positions", ["bottom_outer"]),
        page_box=page_box,
    )
    return pair, candidates, ranked, page_box


def save_debug_capture(frame, cfg: dict, mode: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    capture_dir = ROOT / "debug" / stamp
    suffix = 1
    while capture_dir.exists():
        capture_dir = ROOT / "debug" / f"{stamp}-{suffix}"
        suffix += 1
    capture_dir.mkdir(parents=True)
    cv2.imwrite(str(capture_dir / "raw.png"), frame)
    metadata = {"frame_shape": list(frame.shape), "mode": mode, "pipeline": "book-page-v5", "config": cfg}
    with open(capture_dir / "metadata.json", "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(f"Debug capture saved: {capture_dir}")
    return capture_dir


def _candidate_json(candidate: Candidate) -> dict:
    return {
        "value": candidate.value,
        "score": candidate.score,
        "raw": candidate.raw,
        "box": candidate.box,
        "token_index": candidate.token_index,
        "token_count": candidate.token_count,
    }


def save_debug_result(
    capture_dir: Path | None,
    pair,
    candidates: list[Candidate],
    ranked: list[RankedCandidate],
    error: str | None = None,
    page_box=None,
) -> None:
    if capture_dir is None:
        return
    result = {
        "pair": list(pair) if pair else None,
        "candidates": [_candidate_json(candidate) for candidate in candidates],
        "ranked": [
            {"rank": item.rank, "spread": list(item.spread), **_candidate_json(item.candidate)}
            for item in ranked[:20]
        ],
        "page_box": list(page_box) if page_box else None,
        "error": error,
    }
    with open(capture_dir / "result.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)


def save_live_debug(frame, cfg: dict, pair, candidates, ranked, error: str | None = None, page_box=None) -> None:
    """Overwrite one live snapshot instead of creating a directory every scan."""
    if not cfg.get("debug"):
        return
    capture_dir = ROOT / "debug" / "live"
    capture_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(capture_dir / "raw.png"), frame)
    metadata = {"frame_shape": list(frame.shape), "mode": "continuous", "pipeline": "book-page-v4", "config": cfg}
    with open(capture_dir / "metadata.json", "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    save_debug_result(capture_dir, pair, candidates, ranked, error, page_box)


def fit_for_display(frame, max_width: int, max_height: int):
    height, width = frame.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    if scale == 1.0:
        return frame.copy(), scale
    resized = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


def shift_spread(pair: tuple[int, int] | None, amount: int) -> tuple[int, int] | None:
    if pair is None or pair[0] + amount <= 0:
        return pair
    return pair[0] + amount, pair[1] + amount


def frame_is_valid(frame, frame_score: float, cfg: dict) -> bool:
    """Reject black, covered and severely blurred frames before expensive OCR."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sample = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
    mean, deviation = cv2.meanStdDev(sample)
    return (
        float(mean[0, 0]) >= float(cfg.get("min_frame_brightness", 18.0))
        and float(deviation[0, 0]) >= float(cfg.get("min_frame_contrast", 8.0))
        and frame_score >= float(cfg.get("min_sharpness", 12.0))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Library Terra continuous book and page observer")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--camera", type=int, help="Override camera index")
    args = parser.parse_args()
    cfg = load_config(args.config)
    cfg["display_width"] = min(int(cfg.get("display_width", 960)), 960)
    cfg["display_height"] = min(int(cfg.get("display_height", 540)), 540)
    cfg["min_ocr_confidence"] = float(cfg.get("min_ocr_confidence", 0.35))
    camera_index = cfg["camera_index"] if args.camera is None else args.camera
    books_root = ROOT / str(cfg.get("books_directory", "books"))
    comment_store = CommentStore(books_root)

    runtime_root = ROOT / str(cfg.get("runtime_directory", "runtime"))
    state_publisher = ReadingStatePublisher(runtime_root)
    publish_reading_state(
        state_publisher,
        None,
        None,
        comment_store,
        status="searching_book",
        source="system",
        event="observer_started",
    )
    state_server = None
    if cfg.get("state_server_enabled", True):
        try:
            state_server = StateHttpServer(
                state_publisher,
                str(cfg.get("state_server_host", "0.0.0.0")),
                int(cfg.get("state_server_port", 8765)),
            )
            state_server.start()
            server_host, server_port = state_server.address
            display_host = "127.0.0.1" if server_host in {"0.0.0.0", "::"} else server_host
            print(f"Reading state API: http://{display_host}:{server_port}/state")
        except OSError as exc:
            print(f"Reading state API disabled: {exc}")

    cover_matcher = CoverMatcher(books_root, cfg)
    book_consensus = BookConsensus(int(cfg.get("cover_confirmations", 2)))
    if cover_matcher.entries:
        print("Registered books: " + ", ".join(book.book_id for book in cover_matcher.entries))
    else:
        print(f"No reference covers found in {books_root}. Run add_book.bat to register one.")

    print("Loading OCR model for the first time may take a moment...")
    engine = RapidOCR()
    cap, warmup_frame, camera_info = open_camera(camera_index, cfg)
    recorder = (
        SessionRecorder(ROOT / "debug", cfg, camera_info)
        if cfg.get("telemetry_enabled", True)
        else None
    )
    if recorder:
        print(f"Session log: {recorder.session_dir}")

    motion = MotionDetector(float(cfg["motion_threshold"]))
    motion_gate = MotionGate(
        float(cfg["settle_seconds"]),
        int(cfg.get("motion_start_frames", 3)),
        int(cfg.get("motion_window_frames", 5)),
    )
    frame_buffer = deque(maxlen=int(cfg.get("sharp_buffer_frames", 12)))
    consensus = PageConsensus(
        int(cfg.get("confirmations_required", 2)),
        int(cfg.get("large_jump_confirmations", 3)),
        int(cfg["max_page_jump"]),
    )
    current: tuple[int, int] | None = None
    predicted: tuple[int, int] | None = None
    state = ScanState.UNCONFIRMED
    manual_request = None
    scan_generation = 0
    last_scan_started = 0.0
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="page-ocr")
    cover_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cover-match")
    scan_future = None
    scan_context = None
    cover_future = None
    cover_context = None
    raw_debug = "Continuous observer waiting for a stable frame..."
    notice = ""
    notice_until = 0.0
    fps_tick = time.monotonic()
    fps_frames = 0
    preview_fps = 0.0
    last_ocr_completed_at: float | None = None
    last_ocr_duration_ms: float | None = None
    last_observation: tuple[int, int] | None = None
    scene_change_started: float | None = None
    last_confirmation_event: int | None = None
    labeled_confirmation_events: set[int] = set()
    saved_reacquire_generations: set[int] = set()
    active_book = None
    no_page_result_streak = 0
    last_cover_started = 0.0
    last_cover_duration_ms: float | None = None
    last_cover_match_id: str | None = None
    cover_search_until = time.monotonic() + float(cfg.get("cover_search_after_motion_seconds", 6.0))
    cover_visible_until = 0.0
    last_comment_refresh = 0.0

    print("Camera ready. S=re-anchor now, A=previous spread, D=next spread, Q=quit.")
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, cfg["display_width"], cfg["display_height"])

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Camera frame read failed.")
            break

        now = time.monotonic()
        raw_moving, motion_score = motion.update(frame)
        motion_started, motion_ended = motion_gate.update(raw_moving, now)
        frame_score = sharpness(frame)
        valid_frame = frame_is_valid(frame, frame_score, cfg)
        fps_frames += 1
        if now - fps_tick >= 1.0:
            preview_fps = fps_frames / (now - fps_tick)
            fps_tick, fps_frames = now, 0

        if raw_moving:
            frame_buffer.clear()
        elif valid_frame:
            frame_buffer.append((frame_score, frame.copy()))
        else:
            frame_buffer.clear()
        if motion_started:
            scan_generation += 1
            manual_request = None
            consensus.scene_changed()
            book_consensus.scene_changed()
            predicted = shift_spread(current, 2)
            state = ScanState.TURNING
            scene_change_started = now
            cover_search_until = now + float(cfg.get("cover_search_after_motion_seconds", 6.0))
            if active_book is not None or current is not None:
                publish_reading_state(
                    state_publisher,
                    active_book,
                    current,
                    comment_store,
                    status="turning",
                    source="motion",
                    event="page_turn_started",
                )
            if recorder:
                recorder.record_scene_change(current)
        if motion_ended:
            if active_book is not None or current is not None:
                publish_reading_state(
                    state_publisher,
                    active_book,
                    current,
                    comment_store,
                    status="recognizing",
                    source="motion",
                    event="page_turn_ended",
                )
            if recorder:
                recorder.record_event("scene_stabilized", confirmed=current, predicted=predicted)
        settled = motion_gate.is_settled(now)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("s"):
            if valid_frame:
                # Re-anchoring also uses consensus: one accidental heading can no
                # longer become the visible page after a single OCR result.
                scan_generation += 1
                consensus.reset()
                manual_request = (scan_generation, frame.copy())
                current = None
                predicted = None
                state = ScanState.VERIFYING
                notice = "RE-ANCHORING - VERIFYING TWO OBSERVATIONS"
                notice_until = now + 5.0
                raw_debug = "Manual full-frame OCR queued..."
                scene_change_started = now
                publish_reading_state(
                    state_publisher,
                    active_book,
                    None,
                    comment_store,
                    status="recognizing",
                    source="manual",
                    event="manual_reanchor",
                )
                if recorder:
                    keyframe = recorder.save_keyframe(frame, "manual-reanchor")
                    recorder.record_event("manual_reanchor", keyframe=keyframe)
            else:
                notice = "FRAME INVALID - UNCOVER OR STEADY THE CAMERA"
                notice_until = now + 4.0
        if key == ord("a"):
            base = predicted or current
            current = shift_spread(base, -2)
            predicted = None
            if current:
                consensus.set_confirmed(current)
                state = ScanState.STABLE
                notice = f"MANUAL CORRECTION: P{current[0]} - P{current[1]}"
                notice_until = now + 3.0
                last_confirmation_event = None
                publish_reading_state(
                    state_publisher,
                    active_book,
                    current,
                    comment_store,
                    status="stable",
                    source="manual",
                    event="page_corrected",
                )
                if recorder:
                    recorder.record_manual_correction("previous", current)
        if key == ord("d"):
            base = predicted or current
            current = shift_spread(base, 2)
            predicted = None
            if current:
                consensus.set_confirmed(current)
                state = ScanState.STABLE
                notice = f"MANUAL CORRECTION: P{current[0]} - P{current[1]}"
                notice_until = now + 3.0
                last_confirmation_event = None
                publish_reading_state(
                    state_publisher,
                    active_book,
                    current,
                    comment_store,
                    status="stable",
                    source="manual",
                    event="page_corrected",
                )
                if recorder:
                    recorder.record_manual_correction("next", current)
        if key in (ord("y"), ord("n")):
            label = "correct" if key == ord("y") else "incorrect"
            if recorder and current and last_confirmation_event is not None:
                if last_confirmation_event in labeled_confirmation_events:
                    notice = "THIS CONFIRMATION IS ALREADY LABELED"
                else:
                    label_frame = recorder.save_keyframe(frame, f"label-{label}")
                    recorder.record_label(label, current, last_confirmation_event, label_frame)
                    labeled_confirmation_events.add(last_confirmation_event)
                    notice = f"TEST LABEL: {label.upper()} P{current[0]} - P{current[1]}"
                notice_until = now + 3.0
            else:
                notice = "NO AUTOMATIC CONFIRMATION TO LABEL"
                notice_until = now + 3.0
        if key == ord("m"):
            if recorder:
                label_frame = recorder.save_keyframe(frame, "label-missed")
                recorder.record_label("missed", current, last_confirmation_event, label_frame)
                notice = "TEST LABEL: MISSED PAGE"
                notice_until = now + 3.0

        if scan_future is not None and scan_future.done():
            result_generation, reanchor, capture_dir, scanned_frame, scan_mode, scan_started_at = scan_context
            completed_at = time.monotonic()
            duration_ms = (completed_at - scan_started_at) * 1000.0
            last_ocr_completed_at = completed_at
            last_ocr_duration_ms = duration_ms
            consensus_before = consensus.snapshot()
            error_text = None
            page_box = None
            try:
                pair, candidates, ranked, page_box = scan_future.result()
                save_debug_result(capture_dir, pair, candidates, ranked, page_box=page_box)
                save_live_debug(scanned_frame, cfg, pair, candidates, ranked, page_box=page_box)
                raw_debug = " ".join(
                    f"{item.candidate.value}->{item.spread[0]}-{item.spread[1]}:{item.rank:.1f}"
                    for item in ranked[:4]
                ) or "No footer-like number found"
                if page_box:
                    raw_debug += f" paper-bottom={page_box[3]}"
            except Exception as exc:
                pair, candidates, ranked = None, [], []
                error_text = str(exc)
                raw_debug = f"OCR error: {error_text}"
                save_debug_result(capture_dir, None, [], [], error_text)
                save_live_debug(scanned_frame, cfg, None, [], [], error_text)
            scan_future = None
            scan_context = None
            changed = False
            decision = "error" if error_text else "no_result"
            if result_generation == scan_generation:
                changed, current = consensus.observe(pair)
                if changed and current:
                    decision = "confirmed"
                    predicted = None
                    state = ScanState.STABLE
                    notice = f"PAGE CONFIRMED: P{pair[0]} - P{pair[1]}"
                    print(notice)
                    notice_until = time.monotonic() + 3.0
                    publish_reading_state(
                        state_publisher,
                        active_book,
                        current,
                        comment_store,
                        status="stable",
                        source="page_ocr",
                        event="page_confirmed",
                    )
                elif consensus.pending is not None:
                    decision = (
                        "pending_started"
                        if consensus_before["pending"] != consensus.pending
                        else "pending_progress"
                    )
                    state = ScanState.VERIFYING
                    raw_debug += f" pending={consensus.pending[0]}-{consensus.pending[1]} {consensus.pending_count}/{consensus.required_count}"
                elif current is not None:
                    if pair == current:
                        predicted = None
                        decision = "stable_match"
                        publish_reading_state(
                            state_publisher,
                            active_book,
                            current,
                            comment_store,
                            status="stable",
                            source="page_ocr",
                            event="page_reconfirmed",
                        )
                    state = ScanState.STABLE
                else:
                    state = ScanState.UNCONFIRMED
                    raw_debug += " continuing automatically"
                if pair is None and not error_text:
                    decision = "no_result"
                if error_text:
                    decision = "error"
            else:
                decision = "stale"

            if result_generation == scan_generation and not error_text:
                if pair is None:
                    no_page_result_streak += 1
                else:
                    no_page_result_streak = 0
            last_observation = pair
            consensus_after = consensus.snapshot()
            keyframe = None
            if recorder:
                max_keyframes = int(cfg.get("telemetry_max_keyframes", 200))
                first_failed_reacquire = (
                    decision == "no_result"
                    and scan_mode == "reacquire"
                    and result_generation not in saved_reacquire_generations
                )
                should_save = decision in {"confirmed", "pending_started", "error"} or first_failed_reacquire
                if should_save and recorder.frame_sequence < max_keyframes:
                    suffix = "none" if pair is None else f"{pair[0]}-{pair[1]}"
                    frame_kind = "reacquire-no-result" if first_failed_reacquire else f"{decision}-{suffix}"
                    keyframe = recorder.save_keyframe(scanned_frame, frame_kind)
                    if first_failed_reacquire:
                        saved_reacquire_generations.add(result_generation)
                event_id = recorder.record_ocr(
                    duration_ms=duration_ms,
                    mode=scan_mode,
                    reanchor=reanchor,
                    generation=result_generation,
                    observation=pair,
                    candidates=[_candidate_json(candidate) for candidate in candidates],
                    ranked=[
                        {"rank": item.rank, "spread": item.spread, **_candidate_json(item.candidate)}
                        for item in ranked[:20]
                    ],
                    decision=decision,
                    consensus_before=consensus_before,
                    consensus_after=consensus_after,
                    state=state.value,
                    page_box=page_box,
                    keyframe=keyframe,
                    error=error_text,
                )
                if changed and current:
                    last_confirmation_event = event_id
                    if scene_change_started is not None:
                        recorder.record_confirmation_delay(completed_at - scene_change_started)
                        scene_change_started = None

        if cover_future is not None and cover_future.done():
            cover_generation, cover_frame, cover_started_at = cover_context
            cover_completed_at = time.monotonic()
            last_cover_duration_ms = (cover_completed_at - cover_started_at) * 1000.0
            cover_error = None
            try:
                cover_match = cover_future.result()
            except Exception as exc:
                cover_match = None
                cover_error = str(exc)
            cover_future = None
            cover_context = None

            cover_changed = False
            cover_decision = "error" if cover_error else "no_match"
            if cover_generation != scan_generation or motion_gate.active:
                cover_decision = "stale"
            elif not cover_error:
                previous_pending_id = book_consensus.pending.book_id if book_consensus.pending else None
                cover_changed, matched_book = book_consensus.observe(cover_match)
                if cover_match is not None:
                    last_cover_match_id = cover_match.book.book_id
                    cover_visible_until = cover_completed_at + float(cfg.get("cover_visible_hold_seconds", 2.5))
                else:
                    last_cover_match_id = None
                if cover_changed and matched_book:
                    cover_decision = "confirmed"
                    active_book = matched_book
                    scan_generation += 1
                    consensus.reset()
                    current = None
                    predicted = None
                    manual_request = None
                    no_page_result_streak = 0
                    last_confirmation_event = None
                    scene_change_started = None
                    state = ScanState.UNCONFIRMED
                    cover_search_until = 0.0
                    notice = f"BOOK MATCHED: {matched_book.book_id}"
                    notice_until = cover_completed_at + 4.0
                    print(f"Book matched: {matched_book.title} ({matched_book.book_id})")
                    publish_reading_state(
                        state_publisher,
                        active_book,
                        None,
                        comment_store,
                        status="book_confirmed",
                        source="cover_match",
                        event="book_confirmed",
                    )
                elif cover_match is not None:
                    if book_consensus.confirmed and cover_match.book.book_id == book_consensus.confirmed.book_id:
                        cover_decision = "stable_match"
                    elif previous_pending_id == cover_match.book.book_id:
                        cover_decision = "pending_progress"
                    else:
                        cover_decision = "pending_started"

            cover_keyframe = None
            if recorder:
                if cover_decision in {"confirmed", "pending_started", "error"}:
                    cover_keyframe = recorder.save_keyframe(cover_frame, f"cover-{cover_decision}")
                match_json = None
                if cover_match is not None:
                    match_json = {
                        "book_id": cover_match.book.book_id,
                        "title": cover_match.book.title,
                        "score": round(cover_match.score, 2),
                        "good_matches": cover_match.good_matches,
                        "inliers": cover_match.inliers,
                        "inlier_ratio": round(cover_match.inlier_ratio, 3),
                        "cover_area_ratio": round(cover_match.cover_area_ratio, 3),
                    }
                recorder.record_cover(
                    duration_ms=last_cover_duration_ms,
                    generation=cover_generation,
                    decision=cover_decision,
                    match=match_json,
                    pending_book=book_consensus.pending.book_id if book_consensus.pending else None,
                    pending_count=book_consensus.pending_count,
                    required_count=book_consensus.confirmations,
                    keyframe=cover_keyframe,
                    error=cover_error,
                )

        if scan_future is None:
            if manual_request is not None:
                request_generation, scan_frame = manual_request
                manual_request = None
                capture_dir = save_debug_capture(scan_frame, cfg, "manual-reanchor")
                scan_future = executor.submit(scan_full_frame, engine, scan_frame, dict(cfg), None, True)
                scan_context = (request_generation, True, capture_dir, scan_frame, "manual", now)
                last_scan_started = now
                state = ScanState.VERIFYING
            elif settled and frame_buffer and now >= cover_visible_until:
                needs_fast_scan = current is None or predicted is not None or consensus.pending is not None
                interval = float(
                    cfg.get("ocr_retry_interval_seconds", 0.25)
                    if needs_fast_scan
                    else cfg.get("ocr_verified_interval_seconds", 1.0)
                )
                if now - last_scan_started < interval:
                    scan_frame = None
                else:
                    scan_score, scan_frame = max(frame_buffer, key=lambda buffered: buffered[0])
                if scan_frame is not None:
                    last_scan_started = now
                    auto_reacquiring = (
                        current is not None
                        and predicted is not None
                        and scene_change_started is not None
                        and now - scene_change_started >= float(cfg.get("auto_reacquire_seconds", 3.0))
                    )
                    reanchor = current is None or auto_reacquiring
                    scan_mode = "reacquire" if auto_reacquiring else "continuous"
                    scan_future = executor.submit(scan_full_frame, engine, scan_frame, dict(cfg), current, reanchor)
                    scan_context = (scan_generation, reanchor, None, scan_frame, scan_mode, now)
                    if predicted is not None:
                        state = ScanState.RECOGNIZING
                    elif current is None or consensus.pending is not None:
                        state = ScanState.VERIFYING
                    prefix = "Auto-reacquire" if auto_reacquiring else "Continuous"
                    raw_debug = f"{prefix} OCR running; best sharpness={scan_score:.0f}"

        should_search_cover = (
            bool(cover_matcher.entries)
            and settled
            and bool(frame_buffer)
            and (current is None or no_page_result_streak >= 2 or now < cover_search_until)
        )
        if cover_future is None and should_search_cover:
            cover_interval = float(cfg.get("cover_match_interval_seconds", 1.2))
            if now - last_cover_started >= cover_interval:
                _, cover_frame = max(frame_buffer, key=lambda buffered: buffered[0])
                last_cover_started = now
                cover_future = cover_executor.submit(cover_matcher.match, cover_frame)
                cover_context = (scan_generation, cover_frame, now)

        if (
            state == ScanState.STABLE
            and current is not None
            and now - last_comment_refresh >= float(cfg.get("comment_refresh_interval_seconds", 1.0))
        ):
            last_comment_refresh = now
            publish_reading_state(
                state_publisher,
                active_book,
                current,
                comment_store,
                status="stable",
                source="comment_store",
                event="comment_refreshed",
            )

        if predicted is not None and state in (ScanState.TURNING, ScanState.RECOGNIZING, ScanState.VERIFYING, ScanState.UNCONFIRMED):
            page_text = f"ESTIMATED P{predicted[0]} - P{predicted[1]}"
        elif current is not None:
            page_text = f"P{current[0]} - P{current[1]}"
        else:
            page_text = "PAGE UNCONFIRMED"
        book_id = active_book.book_id if active_book else "UNMATCHED"
        if now < cover_visible_until and active_book:
            page_text = f"BOOK {book_id} | COVER MATCHED"
        else:
            page_text = f"BOOK {book_id} | {page_text}"
        cv2.putText(
            frame,
            f"{state.value} view={preview_fps:.0f}fps motion={motion_score:.1f} turn={int(motion_gate.active)} sharp={frame_score:.0f}",
            (20, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            .70,
            (30, 240, 240),
            2,
        )
        cv2.putText(frame, page_text, (20, 72), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 240, 80), 2)
        ocr_age = "never" if last_ocr_completed_at is None else f"{now - last_ocr_completed_at:.1f}s ago"
        ocr_duration = "-" if last_ocr_duration_ms is None else f"{last_ocr_duration_ms:.0f}ms"
        observation_text = "none" if last_observation is None else f"{last_observation[0]}-{last_observation[1]}"
        pending_text = (
            "none"
            if consensus.pending is None
            else f"{consensus.pending[0]}-{consensus.pending[1]} {consensus.pending_count}/{consensus.required_count}"
        )
        cv2.putText(
            frame,
            f"OCR last={ocr_age} took={ocr_duration} observed={observation_text} pending={pending_text}",
            (20, 105),
            cv2.FONT_HERSHEY_SIMPLEX,
            .52,
            (255, 255, 255),
            1,
        )
        if recorder:
            counters = recorder.counters
            stats_text = (
                f"SESSION scans={counters['ocr_scans']} confirmed={counters['page_confirmations']} "
                f"no-result={counters['ocr_no_result']} cover={counters['cover_matches']}/{counters['cover_scans']}"
            )
            cv2.putText(frame, stats_text, (20, 130), cv2.FONT_HERSHEY_SIMPLEX, .48, (220, 220, 220), 1)
        cv2.putText(
            frame,
            f"{camera_info}   S=re-anchor  A=previous  D=next  Q=quit",
            (20, frame.shape[0] - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            .58,
            (255, 255, 255),
            2,
        )
        if cfg.get("debug") and raw_debug:
            cv2.putText(frame, raw_debug[:150], (20, 155), cv2.FONT_HERSHEY_SIMPLEX, .45, (190, 220, 255), 1)
        if time.monotonic() < notice_until:
            text_size = cv2.getTextSize(notice, cv2.FONT_HERSHEY_SIMPLEX, .68, 2)[0]
            cv2.rectangle(frame, (12, 168), (32 + text_size[0], 208), (20, 20, 20), -1)
            cv2.putText(frame, notice, (22, 196), cv2.FONT_HERSHEY_SIMPLEX, .68, (0, 255, 255), 2)
        cv2.putText(
            frame,
            "TEST LABELS: Y=correct  N=incorrect  M=missed",
            (20, frame.shape[0] - 47),
            cv2.FONT_HERSHEY_SIMPLEX,
            .50,
            (180, 220, 255),
            1,
        )

        display_frame, _ = fit_for_display(frame, cfg["display_width"], cfg["display_height"])
        cv2.imshow(WINDOW_NAME, display_frame)

    cap.release()
    executor.shutdown(wait=False, cancel_futures=True)
    cover_executor.shutdown(wait=False, cancel_futures=True)
    if recorder:
        recorder.close()
        print(f"Session summary: {recorder.summary_path}")
    publish_reading_state(
        state_publisher,
        active_book,
        current,
        comment_store,
        status="offline",
        source="system",
        event="observer_stopped",
    )
    if state_server:
        state_server.close()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
