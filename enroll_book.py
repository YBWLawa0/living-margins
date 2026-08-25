from __future__ import annotations

import argparse
import base64
import json
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
from rapidocr_onnxruntime import RapidOCR

from app import ROOT, fit_for_display, load_config, open_camera
from library_terra.enrollment import (
    fallback_cover_crop,
    find_cover_quad,
    make_book_id,
    recognize_cover,
    warp_cover,
)


WINDOW_NAME = "Library Terra - Capture Book Cover"


def capture_cover(cfg: dict, camera_index: int):
    cap, first_frame, _ = open_camera(camera_index, cfg)
    frame = first_frame
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, min(int(cfg.get("display_width", 960)), 960), min(int(cfg.get("display_height", 540)), 540))
    try:
        while True:
            ok, next_frame = cap.read()
            if ok and next_frame is not None:
                frame = next_frame
            quad = find_cover_quad(frame)
            preview = frame.copy()
            if quad is not None:
                cv2.polylines(preview, [quad.astype(int)], True, (40, 240, 80), 3)
                status = "COVER FOUND - SPACE TO CAPTURE"
            else:
                _, guide = fallback_cover_crop(frame)
                cv2.rectangle(preview, (guide[0], guide[1]), (guide[2], guide[3]), (0, 220, 255), 3)
                status = "ALIGN COVER IN GUIDE - SPACE TO CAPTURE"
            cv2.rectangle(preview, (12, 12), (700, 62), (15, 15, 15), -1)
            cv2.putText(preview, status, (24, 45), cv2.FONT_HERSHEY_SIMPLEX, .72, (255, 255, 255), 2)
            cv2.putText(preview, "Q / ESC = cancel", (20, preview.shape[0] - 22), cv2.FONT_HERSHEY_SIMPLEX, .58, (255, 255, 255), 2)
            display, _ = fit_for_display(preview, 960, 540)
            cv2.imshow(WINDOW_NAME, display)
            key = cv2.waitKey(1) & 0xFF
            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                return None
            if key in (ord("q"), 27):
                return None
            if key == 32:
                if quad is not None:
                    return warp_cover(frame, quad)
                return fallback_cover_crop(frame)[0]
    finally:
        cap.release()
        cv2.destroyWindow(WINDOW_NAME)


def review_cover(cover, suggested_title: str, ocr_lines, books_root: Path) -> tuple[str, str, str]:
    root = tk.Tk()
    root.title("Library Terra - 审查书籍信息")
    root.resizable(False, False)
    decision = {"action": "cancel", "title": "", "book_id": ""}

    preview, _ = fit_for_display(cover, 360, 460)
    ok, encoded = cv2.imencode(".png", preview)
    photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()).decode("ascii")) if ok else None

    outer = ttk.Frame(root, padding=14)
    outer.grid(row=0, column=0)
    if photo is not None:
        image_label = ttk.Label(outer, image=photo)
        image_label.image = photo
        image_label.grid(row=0, column=0, rowspan=8, padx=(0, 18), sticky="n")

    ttk.Label(outer, text="OCR 已完成，请修改错误后确认", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=1, columnspan=2, sticky="w")
    ttk.Label(outer, text="书名").grid(row=1, column=1, sticky="w", pady=(14, 4))
    title_var = tk.StringVar(value=suggested_title)
    title_entry = ttk.Entry(outer, textvariable=title_var, width=46)
    title_entry.grid(row=2, column=1, columnspan=2, sticky="ew")

    ttk.Label(outer, text="系统编号（自动生成，无需修改）").grid(row=3, column=1, sticky="w", pady=(12, 4))
    id_time = datetime.now()
    id_var = tk.StringVar(value=make_book_id(suggested_title, id_time))
    ttk.Entry(outer, textvariable=id_var, width=46, state="readonly").grid(row=4, column=1, columnspan=2, sticky="ew")

    def refresh_id(*_args) -> None:
        id_var.set(make_book_id(title_var.get().strip(), id_time))

    title_var.trace_add("write", refresh_id)

    ttk.Label(outer, text="OCR 识别到的全部文字").grid(row=5, column=1, sticky="w", pady=(12, 4))
    text = tk.Text(outer, width=48, height=10, wrap="word")
    text.grid(row=6, column=1, columnspan=2, sticky="ew")
    text.insert("1.0", "\n".join(f"{line.text}  ({line.confidence:.0%})" for line in ocr_lines) or "未识别到文字，请手动填写书名。")
    text.configure(state="disabled")

    def confirm() -> None:
        title = title_var.get().strip()
        if not title:
            messagebox.showwarning("需要书名", "请填写或修正书名。", parent=root)
            return
        base_id = id_var.get()
        book_id = base_id
        suffix = 2
        while (books_root / book_id / "book.json").exists():
            book_id = f"{base_id}-{suffix}"
            suffix += 1
        decision.update(action="confirm", title=title, book_id=book_id)
        root.destroy()

    def retake() -> None:
        decision["action"] = "retake"
        root.destroy()

    ttk.Button(outer, text="确认入库", command=confirm).grid(row=7, column=1, sticky="ew", pady=(14, 0), padx=(0, 6))
    ttk.Button(outer, text="重新拍照", command=retake).grid(row=7, column=2, sticky="ew", pady=(14, 0), padx=(6, 0))
    ttk.Button(outer, text="取消", command=root.destroy).grid(row=8, column=1, columnspan=2, sticky="ew", pady=(8, 0))
    title_entry.focus_set()
    title_entry.selection_range(0, tk.END)
    root.mainloop()
    return decision["action"], decision["title"], decision["book_id"]


def save_reviewed_book(books_root: Path, book_id: str, title: str, cover, ocr_lines) -> Path:
    book_dir = books_root / book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    cover_path = book_dir / "cover.jpg"
    if not cv2.imwrite(str(cover_path), cover, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        raise RuntimeError(f"Cannot save cover: {cover_path}")
    metadata = {
        "id": book_id,
        "title": title,
        "cover": cover_path.name,
        "reviewed": True,
        "source": "camera-ocr-enrollment",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ocr_lines": [{"text": line.text, "confidence": round(line.confidence, 4)} for line in ocr_lines],
    }
    metadata_path = book_dir / "book.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Photograph, OCR and review a book cover")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--camera", type=int)
    args = parser.parse_args()
    cfg = load_config(args.config)
    camera_index = int(cfg.get("camera_index", 0)) if args.camera is None else args.camera
    books_root = ROOT / str(cfg.get("books_directory", "books"))
    engine = None

    while True:
        cover = capture_cover(cfg, camera_index)
        if cover is None:
            return 0
        print("Loading OCR and reading the cover...")
        if engine is None:
            engine = RapidOCR()
        title, lines = recognize_cover(engine, cover, float(cfg.get("cover_ocr_min_confidence", 0.35)))
        action, reviewed_title, book_id = review_cover(cover, title, lines, books_root)
        if action == "cancel":
            return 0
        if action == "retake":
            continue
        metadata_path = save_reviewed_book(books_root, book_id, reviewed_title, cover, lines)
        notification = tk.Tk()
        notification.withdraw()
        messagebox.showinfo("录入完成", f"已添加《{reviewed_title}》\n{metadata_path}", parent=notification)
        notification.destroy()
        print(f"Added book: {reviewed_title} ({book_id})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
