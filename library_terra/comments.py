from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class PageComment:
    comment_id: str
    page: int
    text: str
    author: str
    page_end: int | None = None
    priority: int = 0
    enabled: bool = True

    def state_value(self) -> dict[str, Any]:
        value = {
            "id": self.comment_id,
            "page": self.page,
            "text": self.text,
            "author": self.author,
        }
        if self.page_end is not None and self.page_end != self.page:
            value["page_end"] = self.page_end
        return value


class CommentStore:
    """Store page comments beside each registered book and select one for a spread."""

    def __init__(self, books_root: Path):
        self.books_root = books_root

    def comments_path(self, book_id: str) -> Path:
        if not book_id or Path(book_id).name != book_id or any(separator in book_id for separator in ("/", "\\")):
            raise ValueError("invalid book id")
        book_dir = self.books_root / book_id
        if not book_dir.is_dir() or not (book_dir / "book.json").is_file():
            raise ValueError(f"book does not exist: {book_id}")
        return book_dir / "comments.json"

    def _read_records(self, book_id: str, *, strict: bool = False) -> list[dict[str, Any]]:
        path = self.comments_path(book_id)
        if not path.exists():
            return []
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            records = document.get("comments", [])
            if not isinstance(records, list):
                raise ValueError("comments must be a list")
            return [record for record in records if isinstance(record, dict)]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            if strict:
                raise
            return []

    @staticmethod
    def _parse(record: dict[str, Any]) -> PageComment | None:
        try:
            comment_id = str(record["id"]).strip()
            page = int(record["page"])
            page_end = int(record.get("page_end", page))
            text = str(record["text"]).strip()
            author = str(record.get("author") or "匿名读者").strip()
            priority = int(record.get("priority", 0))
            enabled = bool(record.get("enabled", True))
        except (KeyError, TypeError, ValueError):
            return None
        if not comment_id or page <= 0 or page_end < page or not text:
            return None
        return PageComment(
            comment_id,
            page,
            text,
            author or "匿名读者",
            page_end,
            priority,
            enabled,
        )

    def list(self, book_id: str) -> list[PageComment]:
        comments = [self._parse(record) for record in self._read_records(book_id)]
        return sorted((comment for comment in comments if comment is not None), key=lambda item: (item.page, -item.priority, item.comment_id))

    def select(self, book_id: str | None, pages: tuple[int, int] | list[int] | None) -> PageComment | None:
        if not book_id or not pages:
            return None
        page_order = {int(page): index for index, page in enumerate(pages)}
        candidates = []
        for comment in self.list(book_id):
            end = comment.page_end if comment.page_end is not None else comment.page
            overlaps = [page for page in page_order if comment.page <= page <= end]
            if comment.enabled and overlaps:
                candidates.append((comment, min(page_order[page] for page in overlaps)))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0].priority, item[1], item[0].comment_id))
        return candidates[0][0]

    def upsert(
        self,
        book_id: str,
        *,
        page: int,
        page_end: int | None = None,
        text: str,
        author: str = "匿名读者",
        priority: int = 0,
        enabled: bool = True,
        comment_id: str | None = None,
    ) -> PageComment:
        page = int(page)
        page_end = page if page_end is None else int(page_end)
        text = text.strip()
        author = author.strip() or "匿名读者"
        if page <= 0 or page_end < page:
            raise ValueError("page range is invalid")
        if not text:
            raise ValueError("comment text is required")

        records = self._read_records(book_id, strict=True)
        now = _timestamp()
        target_id = comment_id or f"comment-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        existing_index = next((index for index, record in enumerate(records) if record.get("id") == target_id), None)
        created_at = records[existing_index].get("created_at", now) if existing_index is not None else now
        record = {
            "id": target_id,
            "page": page,
            "page_end": page_end,
            "text": text,
            "author": author,
            "priority": int(priority),
            "enabled": bool(enabled),
            "created_at": created_at,
            "updated_at": now,
        }
        if existing_index is None:
            records.append(record)
        else:
            records[existing_index] = record
        self._write(book_id, records)
        return self._parse(record)  # type: ignore[return-value]

    def delete(self, book_id: str, comment_id: str) -> bool:
        records = self._read_records(book_id, strict=True)
        remaining = [record for record in records if str(record.get("id")) != comment_id]
        if len(remaining) == len(records):
            return False
        self._write(book_id, remaining)
        return True

    def _write(self, book_id: str, records: list[dict[str, Any]]) -> None:
        path = self.comments_path(book_id)
        document = {"schema_version": 1, "comments": records}
        temporary = path.parent / f".comments-{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
