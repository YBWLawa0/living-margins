from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
from rapidocr_onnxruntime import RapidOCR

from app import PageConsensus, frame_is_valid, load_config, scan_full_frame
from library_terra.books import BookConsensus, CoverMatcher
from library_terra.comments import CommentStore
from library_terra.vision import sharpness


FRAME_NAME_PATTERN = re.compile(r"^user-(?P<user_id>\d+)-session-(?P<session_id>\d+)\.jpg$")


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RelayPublisher:
    def __init__(
        self,
        path: Path,
        *,
        user_id: int | None = None,
        session_id: int | None = None,
    ):
        self.path = path
        self.user_id = user_id
        self.session_id = session_id
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
                temporary.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
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
        if self.user_id is not None:
            state["user_id"] = self.user_id
        if self.session_id is not None:
            state["reading_session_id"] = self.session_id
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)
        self.semantic = semantic
        print(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "revision": self.revision,
                    "status": status,
                    "pages": semantic["pages"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return True


def frame_channel(path: Path) -> tuple[int, int] | None:
    match = FRAME_NAME_PATTERN.fullmatch(path.name)
    if match is None:
        return None
    return int(match.group("user_id")), int(match.group("session_id"))


def newest_frame(root: Path) -> Path | None:
    frames = list(root.glob("user-*.jpg"))
    return max(
        frames,
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        default=None,
    )


def newest_frames(root: Path) -> dict[tuple[int, int], Path]:
    result: dict[tuple[int, int], Path] = {}
    for path in root.glob("user-*-session-*.jpg"):
        channel = frame_channel(path)
        if channel is None:
            continue
        current = result.get(channel)
        if current is None or (path.stat().st_mtime_ns, path.name) > (
            current.stat().st_mtime_ns,
            current.name,
        ):
            result[channel] = path
    return result


@dataclass
class VisionChannel:
    user_id: int
    session_id: int
    publisher: RelayPublisher
    book_consensus: BookConsensus
    page_consensus: PageConsensus
    active_book: Any = None
    pages: tuple[int, int] | None = None
    last_signature: tuple[str, int, int] | None = None
    last_waiting_publish: float = 0.0


def create_channel(
    user_id: int,
    session_id: int,
    state_root: Path,
    cfg: dict[str, Any],
    comments: CommentStore,
) -> VisionChannel:
    channel = VisionChannel(
        user_id=user_id,
        session_id=session_id,
        publisher=RelayPublisher(
            state_root / f"session-{session_id}.json",
            user_id=user_id,
            session_id=session_id,
        ),
        book_consensus=BookConsensus(int(cfg.get("cover_confirmations", 2))),
        page_consensus=PageConsensus(
            int(cfg.get("confirmations_required", 2)),
            int(cfg.get("large_jump_confirmations", 3)),
            int(cfg.get("max_page_jump", 12)),
        ),
    )
    channel.publisher.publish(
        book=None,
        pages=None,
        status="waiting_camera",
        event="worker_started",
        comment_store=comments,
    )
    return channel


def process_frame(
    channel: VisionChannel,
    frame_path: Path,
    *,
    cfg: dict[str, Any],
    engine: RapidOCR,
    cover_matcher: CoverMatcher,
    comments: CommentStore,
) -> None:
    now = time.time()
    if now - frame_path.stat().st_mtime > 8:
        if now - channel.last_waiting_publish > 5:
            channel.publisher.publish(
                book=channel.active_book,
                pages=channel.pages,
                status="waiting_camera",
                event="camera_stale",
                comment_store=comments,
            )
            channel.last_waiting_publish = now
        return
    stat = frame_path.stat()
    signature = (frame_path.name, stat.st_mtime_ns, stat.st_size)
    if signature == channel.last_signature:
        return
    channel.last_signature = signature
    frame = cv2.imread(str(frame_path))
    if frame is None:
        return
    frame_score = sharpness(frame)
    if not frame_is_valid(frame, frame_score, cfg):
        channel.publisher.publish(
            book=channel.active_book,
            pages=channel.pages,
            status="frame_unstable",
            event="invalid_frame",
            comment_store=comments,
        )
        return

    if channel.active_book is None:
        match = cover_matcher.match(frame)
        changed, matched = channel.book_consensus.observe(match)
        if changed and matched is not None:
            channel.active_book = matched
            channel.page_consensus.reset()
            channel.pages = None
            channel.publisher.publish(
                book=channel.active_book,
                pages=None,
                status="searching_page",
                event="book_confirmed",
                comment_store=comments,
            )
        else:
            channel.publisher.publish(
                book=None,
                pages=None,
                status="searching_book",
                event="cover_scan",
                comment_store=comments,
            )
            return

    try:
        observed, _, _, _ = scan_full_frame(
            engine, frame, cfg, channel.pages, channel.pages is None
        )
        changed, channel.pages = channel.page_consensus.observe(observed)
        if channel.pages is not None:
            channel.publisher.publish(
                book=channel.active_book,
                pages=channel.pages,
                status="stable",
                event="page_confirmed" if changed else "page_reconfirmed",
                comment_store=comments,
            )
        else:
            channel.publisher.publish(
                book=channel.active_book,
                pages=None,
                status="recognizing",
                event="page_scan",
                comment_store=comments,
            )
    except Exception as exc:
        print(f"OCR error session={channel.session_id}: {exc}", flush=True)
        channel.publisher.publish(
            book=channel.active_book,
            pages=channel.pages,
            status="recognizing",
            event="ocr_error",
            comment_store=comments,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize mobile camera frames in the cloud")
    parser.add_argument("--config", default="/app/config.json")
    parser.add_argument(
        "--frames", default=os.environ.get("LM_MOBILE_FRAME_ROOT", "/data/mobile_frames")
    )
    parser.add_argument("--books", default=os.environ.get("LM_BOOKS_ROOT", "/data/books"))
    parser.add_argument(
        "--state-root", default=os.environ.get("LM_RELAY_STATE_ROOT", "/data/relay_states")
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg["debug"] = False
    frames_root, books_root, state_root = (
        Path(args.frames),
        Path(args.books),
        Path(args.state_root),
    )
    state_root.mkdir(parents=True, exist_ok=True)
    comments = CommentStore(books_root)
    cover_matcher = CoverMatcher(books_root, cfg)
    engine = RapidOCR()
    channels: dict[tuple[int, int], VisionChannel] = {}

    while True:
        frames = newest_frames(frames_root)
        for key, frame_path in frames.items():
            channel = channels.get(key)
            if channel is None:
                channel = create_channel(key[0], key[1], state_root, cfg, comments)
                channels[key] = channel
            process_frame(
                channel,
                frame_path,
                cfg=cfg,
                engine=engine,
                cover_matcher=cover_matcher,
                comments=comments,
            )
        time.sleep(0.15)


if __name__ == "__main__":
    raise SystemExit(main())