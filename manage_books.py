from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

import cv2


ROOT = Path(__file__).resolve().parent
BOOKS_ROOT = ROOT / "books"


def add_book(book_id: str, title: str, cover_source: Path, replace: bool) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", book_id):
        raise SystemExit("Book ID may only contain lowercase letters, numbers, hyphens and underscores.")
    cover_source = cover_source.resolve()
    if not cover_source.is_file() or cv2.imread(str(cover_source)) is None:
        raise SystemExit(f"Cannot read cover image: {cover_source}")

    book_dir = BOOKS_ROOT / book_id
    metadata_path = book_dir / "book.json"
    if metadata_path.exists() and not replace:
        raise SystemExit(f"Book already exists: {book_id}. Add --replace to update it.")
    book_dir.mkdir(parents=True, exist_ok=True)
    extension = cover_source.suffix.lower() if cover_source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    destination = book_dir / f"cover{extension}"
    shutil.copy2(cover_source, destination)
    metadata = {"id": book_id, "title": title, "cover": destination.name}
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Added book: {title} ({book_id})")
    print(f"Cover: {destination}")
    return 0


def list_books() -> int:
    metadata_files = sorted(BOOKS_ROOT.glob("*/book.json")) if BOOKS_ROOT.exists() else []
    if not metadata_files:
        print("No books registered.")
        return 0
    for path in metadata_files:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        print(f"{metadata.get('id', path.parent.name)}\t{metadata.get('title', '')}\t{path.parent / metadata.get('cover', 'cover.jpg')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the local Library Terra book catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="Register a reference cover image")
    add_parser.add_argument("--id", required=True, dest="book_id")
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--cover", required=True, type=Path)
    add_parser.add_argument("--replace", action="store_true")
    subparsers.add_parser("list", help="List registered books")
    args = parser.parse_args()
    if args.command == "add":
        return add_book(args.book_id, args.title, args.cover, args.replace)
    return list_books()


if __name__ == "__main__":
    raise SystemExit(main())
