from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np


Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Candidate:
    value: int
    score: float
    box: tuple[int, int, int, int]
    raw: str
    token_index: int = 0
    token_count: int = 1


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    spread: tuple[int, int]
    rank: float


def normalized_box(frame_shape: tuple[int, ...], roi: Iterable[float]) -> tuple[int, int, int, int]:
    h, w = frame_shape[:2]
    x1, y1, x2, y2 = roi
    return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)


def _smooth_box(previous: Box | None, current: Box, weight: float = 0.35) -> Box:
    """Dampen small contour changes so the OCR regions do not jitter."""
    if previous is None:
        return current
    return tuple(round(old * (1.0 - weight) + new * weight) for old, new in zip(previous, current))  # type: ignore[return-value]


def detect_book_box(frame: np.ndarray, min_area_ratio: float = 0.08) -> Box | None:
    """Locate the bright, low-saturation paper region without a fixed ROI."""
    height, width = frame.shape[:2]
    scale = min(1.0, 640.0 / max(width, 1))
    small = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    value = cv2.GaussianBlur(hsv[:, :, 2], (9, 9), 0)
    saturation = hsv[:, :, 1]

    otsu_level, _ = cv2.threshold(value, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    brightness_floor = max(55, min(190, round(otsu_level * 0.88)))
    mask = ((value >= brightness_floor) & (saturation <= 150)).astype(np.uint8) * 255
    close_size = max(9, round(small.shape[1] * 0.025) | 1)
    open_size = max(3, round(small.shape[1] * 0.006) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close_size, close_size), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((open_size, open_size), np.uint8))

    image_area = small.shape[0] * small.shape[1]
    best: tuple[float, tuple[int, int, int, int]] | None = None
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * min_area_ratio:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w < small.shape[1] * 0.18 or h < small.shape[0] * 0.25:
            continue
        rectangularity = area / max(w * h, 1)
        if rectangularity < 0.42:
            continue
        touches_border = int(x <= 2 or y <= 2 or x + w >= small.shape[1] - 2 or y + h >= small.shape[0] - 2)
        score = area * (0.65 + 0.35 * rectangularity) * (1.05 if touches_border else 1.0)
        if best is None or score > best[0]:
            best = score, (x, y, x + w, y + h)

    if best is None:
        return None
    x1, y1, x2, y2 = best[1]
    padding = round(8 * scale)
    x1, y1 = max(0, x1 - padding), max(0, y1 - padding)
    x2, y2 = min(small.shape[1], x2 + padding), min(small.shape[0], y2 + padding)
    inverse = 1.0 / scale
    return round(x1 * inverse), round(y1 * inverse), round(x2 * inverse), round(y2 * inverse)


class PageLocator:
    """Track a live book rectangle and fall back safely when detection is lost."""

    def __init__(self, fallback: Box, hold_frames: int = 20):
        self.fallback = fallback
        self.box: Box | None = None
        self.hold_frames = hold_frames
        self.missed_frames = hold_frames + 1

    def update(self, frame: np.ndarray) -> tuple[Box, bool]:
        detected = detect_book_box(frame)
        if detected is not None:
            self.box = _smooth_box(self.box, detected)
            self.missed_frames = 0
            return self.box, True
        self.missed_frames += 1
        if self.box is not None and self.missed_frames <= self.hold_frames:
            return self.box, False
        return self.fallback, False


def split_book_box(book_box: Box) -> tuple[Box, Box]:
    """Split the dynamically located open-book region into left and right pages."""
    x1, y1, x2, y2 = book_box
    middle = (x1 + x2) // 2
    return (x1, y1, middle, y2), (middle, y1, x2, y2)


def number_boxes(page_box: tuple[int, int, int, int], positions: list[str], width_ratio: float, height_ratio: float, side: str) -> list[tuple[int, int, int, int]]:
    """Return overlapping windows that cover each requested page edge.

    Small overlapping windows keep page numbers large enough for OCR while still
    tolerating horizontal book movement.
    """
    x1, y1, x2, y2 = page_box
    w, h = x2 - x1, y2 - y1
    rh = int(h * height_ratio)
    rw = int(w * max(width_ratio, 0.38))
    starts = [round(x1 + (w - rw) * index / 3) for index in range(4)]
    boxes = []
    for pos in dict.fromkeys("top" if p.startswith("top") else "bottom" for p in positions):
        top = pos.startswith("top")
        by = y1 if top else y2 - rh
        boxes.extend((bx, by, bx + rw, by + rh) for bx in starts)
    return boxes


def preprocess_variants(gray: np.ndarray) -> list[np.ndarray]:
    # Small, slightly defocused digits benefit from enlargement and contrast
    # normalization. Multiple variants are intentional: no single threshold works
    # for both a shadowed left page and a bright right page.
    enlarged = cv2.resize(gray, None, fx=3.5, fy=3.5, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(enlarged)
    sharp = cv2.addWeighted(clahe, 1.8, cv2.GaussianBlur(clahe, (0, 0), 1.6), -0.8, 0)
    otsu = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    adaptive = cv2.adaptiveThreshold(sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 9)
    # Keep the original scale as well. The live locator can produce a tight crop
    # where digits are already large; enlarging those digits may turn an 8 into 0.
    return [gray, enlarged, clahe, sharp, otsu, adaptive]


def sharpness(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def parse_page_numbers(text: str) -> list[int]:
    cleaned = text.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1", "S": "5"}))
    values = (int(match) for match in re.findall(r"(?<!\d)(\d{1,4})(?!\d)", cleaned))
    return list(dict.fromkeys(value for value in values if 0 < value < 3000))


def parse_page_number(text: str) -> int | None:
    """Compatibility helper for callers that only need the first number."""
    values = parse_page_numbers(text)
    return values[0] if values else None


def ocr_candidates(result, min_confidence: float) -> list[Candidate]:
    """Convert full-frame OCR lines into numeric candidates with real text boxes."""
    candidates: list[Candidate] = []
    for item in result or []:
        polygon, raw, confidence = item
        raw, confidence = str(raw), float(confidence)
        if confidence < min_confidence:
            continue
        values = parse_page_numbers(raw)
        if not values:
            continue
        points = np.asarray(polygon, dtype=np.float32).reshape(-1, 2)
        x1, y1 = np.floor(points.min(axis=0)).astype(int)
        x2, y2 = np.ceil(points.max(axis=0)).astype(int)
        for index, value in enumerate(values):
            candidates.append(Candidate(value, confidence, (int(x1), int(y1), int(x2), int(y2)), raw, index, len(values)))
    return candidates


def _spread_for_candidate(candidate: Candidate, horizontal_bounds: tuple[int, int]) -> tuple[int, int]:
    """Infer an even-left/odd-right spread from one visible page number."""
    left, right = horizontal_bounds
    center_x = (candidate.box[0] + candidate.box[2]) / 2
    on_left = center_x < (left + right) / 2
    if on_left:
        return (candidate.value, candidate.value + 1) if candidate.value % 2 == 0 else (candidate.value - 1, candidate.value)
    return (candidate.value - 1, candidate.value) if candidate.value % 2 == 1 else (candidate.value, candidate.value + 1)


def rank_page_candidates(
    candidates: list[Candidate],
    frame_shape: tuple[int, ...],
    previous: tuple[int, int] | None,
    max_jump: int,
    reanchor: bool = False,
    positions: list[str] | None = None,
    page_box: Box | None = None,
) -> list[RankedCandidate]:
    """Rank full-frame numbers using geometry and state without hard-cropping.

    The book locator is intentionally absent: text is always recognized first.
    Geometry is a soft signal, so a slightly underestimated page edge cannot cut
    off the actual page number as it did in the 66-67 field sample.
    """
    height, width = frame_shape[:2]
    positions = positions or ["bottom_outer"]
    allow_bottom = any(position.startswith("bottom") for position in positions)
    allow_top = any(position.startswith("top") for position in positions)
    geometry = page_box or (0, 0, width, height)
    gx1, gy1, gx2, gy2 = geometry
    geometry_width = max(gx2 - gx1, 1)
    geometry_height = max(gy2 - gy1, 1)
    ranked: list[RankedCandidate] = []
    for candidate in candidates:
        x1, y1, x2, y2 = candidate.box
        pixel_x = (x1 + x2) / 2
        pixel_y = (y1 + y2) / 2
        # The locator is a soft coordinate system only. If it underestimates the
        # paper and puts a candidate outside, use the full frame rather than
        # discarding it (the old hard crop lost page 67 in exactly this way).
        inside_soft_box = (
            gx1 - geometry_width * 0.03 <= pixel_x <= gx2 + geometry_width * 0.03
            and gy1 - geometry_height * 0.03 <= pixel_y <= gy2 + geometry_height * 0.03
        )
        if inside_soft_box:
            center_x = (pixel_x - gx1) / geometry_width
            center_y = (pixel_y - gy1) / geometry_height
            horizontal_bounds = (gx1, gx2)
        else:
            center_x = pixel_x / max(width, 1)
            center_y = pixel_y / max(height, 1)
            horizontal_bounds = (0, width)
        edge_strength = min(1.0, abs(center_x - 0.5) * 2.0)
        bottom_strength = max(0.0, min(1.0, (center_y - 0.58) / 0.36)) if allow_bottom else 0.0
        top_strength = max(0.0, min(1.0, (0.42 - center_y) / 0.36)) if allow_top else 0.0
        vertical_strength = max(bottom_strength, top_strength)
        if vertical_strength < 0.30 or edge_strength < 0.20:
            continue

        # A fanned or partly turned book can expose page numbers from sheets
        # underneath the active page. They tend to sit at the extreme outside
        # edge. Keep them visible to the ranker, but prefer a number with a
        # plausible printed margin inside the detected paper bounds.
        border_distance = min(center_x, 1.0 - center_x)
        extreme_edge_penalty = 0.0
        if page_box is not None and inside_soft_box and border_distance < 0.12:
            extreme_edge_penalty = (0.12 - border_distance) / 0.12 * 2.4

        on_left = center_x < 0.5
        expected_token = 0 if on_left else candidate.token_count - 1
        token_bonus = 0.95 if candidate.token_index == expected_token else -0.65
        if candidate.token_count == 1:
            token_bonus += 0.35

        expected_parity = 0 if on_left else 1
        parity_bonus = 0.65 if candidate.value % 2 == expected_parity else -0.25
        small_number_penalty = 0.65 if candidate.value < 10 else 0.0
        spread = _spread_for_candidate(candidate, horizontal_bounds)
        score = (
            candidate.score * 2.0
            + vertical_strength * 1.55
            + edge_strength * 1.15
            + token_bonus
            + parity_bonus
            - small_number_penalty
            - extreme_edge_penalty
        )

        # Previous state is only positive evidence for routine tracking. A large
        # jump is not penalized here because PageConsensus already requires more
        # repeated observations for it. Penalizing in both layers caused a real
        # 90-91 -> 110-111 jump to remain below the visual acceptance threshold.
        if previous is not None and not reanchor:
            jump = abs(spread[0] - previous[0])
            if jump in (0, 2):
                score += 1.35
            elif jump <= max_jump:
                score += max(0.0, 0.8 - jump / max(max_jump, 1))
        ranked.append(RankedCandidate(candidate, spread, score))
    return sorted(ranked, key=lambda item: item.rank, reverse=True)


def choose_page_spread(
    candidates: list[Candidate],
    frame_shape: tuple[int, ...],
    previous: tuple[int, int] | None,
    max_jump: int,
    reanchor: bool = False,
    positions: list[str] | None = None,
    page_box: Box | None = None,
) -> tuple[tuple[int, int] | None, list[RankedCandidate]]:
    ranked = rank_page_candidates(candidates, frame_shape, previous, max_jump, reanchor, positions, page_box)
    if not ranked or ranked[0].rank < 3.75:
        return None, ranked
    if len(ranked) > 1 and ranked[1].spread != ranked[0].spread and ranked[0].rank - ranked[1].rank < 0.30:
        return None, ranked
    return ranked[0].spread, ranked


def choose_pair(left: list[Candidate], right: list[Candidate], previous: tuple[int, int] | None, max_jump: int, allow_single: bool = False) -> tuple[int, int] | None:
    pairs: list[tuple[float, int, int]] = []
    for l in left:
        for r in right:
            if r.value != l.value + 1:
                continue
            score = l.score + r.score + 1.5
            if l.value % 2 == 0 and r.value % 2 == 1:
                score += 0.5
            if previous:
                jump = abs(l.value - previous[0])
                if jump > max_jump:
                    continue
                score += max(0.0, 1.0 - jump / max(max_jump, 1))
            pairs.append((score, l.value, r.value))
    if pairs:
        _, lv, rv = max(pairs)
        return lv, rv

    if not allow_single:
        return None

    # Optional fallback. It is disabled by default because chapter/list numbers can
    # otherwise masquerade as a page number, as seen in the first field test.
    singles = [(c.score, c.value, "left") for c in left] + [(c.score, c.value, "right") for c in right]
    for _, value, side in sorted(singles, reverse=True):
        pair = (value, value + 1) if side == "left" else (value - 1, value)
        if pair[0] <= 0 or pair[0] % 2 != 0 or pair[1] % 2 != 1:
            continue
        if previous and abs(pair[0] - previous[0]) > max_jump:
            continue
        return pair
    return None


class MotionDetector:
    def __init__(self, threshold: float):
        self.threshold = threshold
        self.previous: np.ndarray | None = None

    def update(self, frame: np.ndarray) -> tuple[bool, float]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (320, 180))
        small = cv2.GaussianBlur(small, (9, 9), 0)
        if self.previous is None:
            self.previous = small
            return False, 0.0
        score = float(cv2.mean(cv2.absdiff(self.previous, small))[0])
        self.previous = small
        return score >= self.threshold, score


class MotionGate:
    """Turn noisy per-frame motion into one stable scene-change episode."""

    def __init__(self, settle_seconds: float, start_frames: int = 3, window_frames: int = 5):
        self.settle_seconds = max(0.0, settle_seconds)
        self.start_frames = max(1, start_frames)
        self.samples: deque[bool] = deque(maxlen=max(self.start_frames, window_frames))
        self.active = False
        self.last_activity = time.monotonic()

    def update(self, raw_motion: bool, now: float) -> tuple[bool, bool]:
        """Return (episode_started, episode_ended)."""
        self.samples.append(raw_motion)
        if raw_motion:
            self.last_activity = now

        started = False
        ended = False
        if not self.active and sum(self.samples) >= self.start_frames:
            self.active = True
            started = True
        elif self.active and now - self.last_activity >= self.settle_seconds:
            self.active = False
            self.samples.clear()
            ended = True
        return started, ended

    def is_settled(self, now: float) -> bool:
        return not self.active and now - self.last_activity >= self.settle_seconds
