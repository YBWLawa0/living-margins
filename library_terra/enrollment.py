from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

import cv2
import numpy as np


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: tuple[int, int, int, int]
    title_score: float


def order_quad(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def find_cover_quad(frame: np.ndarray, min_area_ratio: float = 0.16) -> np.ndarray | None:
    """Find the largest plausible four-sided cover in a camera frame."""
    height, width = frame.shape[:2]
    scale = min(1.0, 900.0 / max(width, height, 1))
    small = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 40, 120)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    image_area = small.shape[0] * small.shape[1]
    best: tuple[float, np.ndarray] | None = None
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * min_area_ratio or area > image_area * 0.97:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        rectangle = cv2.minAreaRect(polygon)
        rw, rh = rectangle[1]
        if min(rw, rh) < 1:
            continue
        aspect = max(rw, rh) / min(rw, rh)
        if not 1.05 <= aspect <= 2.4:
            continue
        rectangularity = area / max(rw * rh, 1)
        score = area * min(rectangularity, 1.0)
        if best is None or score > best[0]:
            best = score, polygon.reshape(4, 2).astype(np.float32) / scale
    return order_quad(best[1]) if best else None


def warp_cover(frame: np.ndarray, quad: np.ndarray) -> np.ndarray:
    top_left, top_right, bottom_right, bottom_left = order_quad(quad)
    width = round(max(np.linalg.norm(top_right - top_left), np.linalg.norm(bottom_right - bottom_left)))
    height = round(max(np.linalg.norm(bottom_left - top_left), np.linalg.norm(bottom_right - top_right)))
    width, height = max(width, 120), max(height, 160)
    target = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    matrix = cv2.getPerspectiveTransform(np.float32([top_left, top_right, bottom_right, bottom_left]), target)
    return cv2.warpPerspective(frame, matrix, (width, height))


def fallback_cover_crop(frame: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Return a centered portrait crop matching the on-screen fallback guide."""
    height, width = frame.shape[:2]
    crop_height = round(height * 0.86)
    crop_width = min(round(width * 0.62), round(crop_height * 0.78))
    x1 = (width - crop_width) // 2
    y1 = (height - crop_height) // 2
    box = x1, y1, x1 + crop_width, y1 + crop_height
    return frame[box[1] : box[3], box[0] : box[2]].copy(), box


def extract_ocr_lines(result, image_shape: tuple[int, ...], min_confidence: float = 0.35) -> list[OcrLine]:
    height, width = image_shape[:2]
    lines: list[OcrLine] = []
    for item in result or []:
        polygon, raw_text, confidence = item
        text = re.sub(r"\s+", " ", str(raw_text)).strip(" -_|·•")
        confidence = float(confidence)
        if confidence < min_confidence or len(text) < 2:
            continue
        points = np.asarray(polygon, dtype=np.float32).reshape(-1, 2)
        x1, y1 = np.floor(points.min(axis=0)).astype(int)
        x2, y2 = np.ceil(points.max(axis=0)).astype(int)
        line_height = max(y2 - y1, 1) / max(height, 1)
        line_width = max(x2 - x1, 1) / max(width, 1)
        center_x = (x1 + x2) / 2 / max(width, 1)
        center_y = (y1 + y2) / 2 / max(height, 1)
        center_bonus = max(0.0, 1.0 - abs(center_x - 0.5) * 1.5)
        bottom_penalty = max(0.0, (center_y - 0.82) * 3.0)
        score = confidence * 1.4 + line_height * 10.0 + line_width * 1.2 + center_bonus * 0.35 - bottom_penalty
        lines.append(OcrLine(text, confidence, (int(x1), int(y1), int(x2), int(y2)), score))
    return sorted(lines, key=lambda line: line.title_score, reverse=True)


def suggest_title(lines: list[OcrLine], image_shape: tuple[int, ...]) -> str:
    if not lines:
        return ""
    height = image_shape[0]
    primary = lines[0]
    primary_height = max(primary.box[3] - primary.box[1], 1)
    selected = [primary]
    for line in lines[1:]:
        line_height = max(line.box[3] - line.box[1], 1)
        vertical_gap = min(abs(line.box[1] - primary.box[3]), abs(primary.box[1] - line.box[3]))
        if line_height >= primary_height * 0.58 and vertical_gap <= height * 0.22:
            selected.append(line)
        if len(selected) >= 3:
            break
    selected.sort(key=lambda line: (line.box[1], line.box[0]))
    return " ".join(line.text for line in selected)


def make_book_id(title: str, now: datetime | None = None) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:42]
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{stamp[-6:]}" if slug else f"book-{stamp}"


def recognize_cover(engine, cover: np.ndarray, min_confidence: float = 0.35) -> tuple[str, list[OcrLine]]:
    """OCR a cover and return an editable title suggestion plus all useful lines."""
    attempts: list[tuple[str, list[OcrLine], float]] = []
    for image in (cover, cv2.rotate(cover, cv2.ROTATE_180)):
        result, _ = engine(image, use_det=True, use_cls=True, use_rec=True)
        lines = extract_ocr_lines(result, image.shape, min_confidence)
        title = suggest_title(lines, image.shape)
        quality = sum(line.title_score for line in lines[:3])
        attempts.append((title, lines, quality))
    title, lines, _ = max(attempts, key=lambda attempt: attempt[2])
    return title, lines
