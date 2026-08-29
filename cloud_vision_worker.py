from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
from rapidocr_onnxruntime import RapidOCR

from app import PageConsensus, frame_is_valid, load_config, scan_full_frame
from library_terra.books import BookConsensus, CoverMatcher
from library_terra.comments import CommentStore
from library_terra.vision import sharpness


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RelayPublisher:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.revision = 0
        self.semantic: dict[str, Any] | None = None
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            self.revision = max(0, int(previous.get("revision", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def publish(
        self,
        *,
        book,
        pages: tuple[int, int] | None,
        status: str,
        event: str,
        comment_store: CommentStore,
    ) -> bool:
        comment = None
        if book is not None and pages is not None:
            selected = comment_store.select(book.book_id, pages)
            comment = selected.state_value() if selected is not None else None
        semantic = {
            "book_id": book.book_id if book else None,
            "title": book.title if book else None,
            "pages": list(pages) if pages else None,
            "status": status,
            "comment": comment,
        }
        if semantic == self.semantic:
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                state["_relay_received_at"] = time.time()
                temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            return False
        self.revision += 1
        state = {
            "schema_version": 1,
            "revision": self.revision,
            **semantic,
            "source": "mobile_camera_cloud",
            "event": event,
            "updated_at": timestamp(),
            "_relay_received_at": time.time(),
        }
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        self.semantic = semantic
        print(json.dumps({"revision": self.revision, "status": status, "pages": semantic["pages"]}, ensure_ascii=False), flush=True)
        return True


def newest_frame(root: Path) -> Path | None:
    frames = list(root.glob("user-*.jpg"))
    return max(frames, key=lambda path: (path.stat().st_mtime_ns, path.name), default=None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize mobile camera frames in the cloud")
    parser.add_argument("--config", default="/app/config.json")
    parser.add_argument("--frames", default=os.environ.get("LM_MOBILE_FRAME_ROOT", "/data/mobile_frames"))
    parser.add_argument("--books", default=os.environ.get("LM_BOOKS_ROOT", "/data/books"))
    parser.add_argument("--state", default=os.environ.get("LM_RELAY_STATE_PATH", "/data/relay_state.json"))
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg["debug"] = False
    frames_root, books_root = Path(args.frames), Path(args.books)
    publisher = RelayPublisher(Path(args.state))
    comments = CommentStore(books_root)
    cover_matcher = CoverMatcher(books_root, cfg)
    book_consensus = BookConsensus(int(cfg.get("cover_confirmations", 2)))
    page_consensus = PageConsensus(
        int(cfg.get("confirmations_required", 2)),
        int(cfg.get("large_jump_confirmations", 3)),
        int(cfg.get("max_page_jump", 12)),
    )
    engine = RapidOCR()
    active_book = None
    pages: tuple[int, int] | None = None
    last_signature: tuple[str, int, int] | None = None
    last_waiting_publish = 0.0
    publisher.publish(book=None, pages=None, status="waiting_camera", event="worker_started", comment_store=comments)

    while True:
        frame_path = newest_frame(frames_root)
        now = time.time()
        if frame_path is None or now - frame_path.stat().st_mtime > 8:
            if now - last_waiting_publish > 5:
                publisher.publish(book=active_book, pages=pages, status="waiting_camera", event="camera_stale", comment_store=comments)
                last_waiting_publish = now
            time.sleep(0.35)
            continue
        stat = frame_path.stat()
        signature = (frame_path.name, stat.st_mtime_ns, stat.st_size)
        if signature == last_signature:
            time.sleep(0.15)
            continue
        last_signature = signature
        frame = cv2.imread(str(frame_path))
        if frame is None:
            time.sleep(0.2)
            continue
        frame_score = sharpness(frame)
        if not frame_is_valid(frame, frame_score, cfg):
            publisher.publish(book=active_book, pages=pages, status="frame_unstable", event="invalid_frame", comment_store=comments)
            continue

        if active_book is None:
            match = cover_matcher.match(frame)
            changed, matched = book_consensus.observe(match)
            if changed and matched is not None:
                active_book = matched
                page_consensus.reset()
                pages = None
                publisher.publish(book=active_book, pages=None, status="searching_page", event="book_confirmed", comment_store=comments)
            else:
                publisher.publish(book=None, pages=None, status="searching_book", event="cover_scan", comment_store=comments)
                continue

        try:
            observed, _, _, _ = scan_full_frame(engine, frame, cfg, pages, pages is None)
            changed, pages = page_consensus.observe(observed)
            if pages is not None:
                publisher.publish(book=active_book, pages=pages, status="stable", event="page_confirmed" if changed else "page_reconfirmed", comment_store=comments)
            else:
                publisher.publish(book=active_book, pages=None, status="recognizing", event="page_scan", comment_store=comments)
        except Exception as exc:
            print(f"OCR error: {exc}", flush=True)
            publisher.publish(book=active_book, pages=pages, status="recognizing", event="ocr_error", comment_store=comments)


if __name__ == "__main__":
    raise SystemExit(main())
