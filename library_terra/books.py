from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class BookEntry:
    book_id: str
    title: str
    cover_path: Path


@dataclass(frozen=True)
class CoverMatch:
    book: BookEntry
    score: float
    good_matches: int
    inliers: int
    inlier_ratio: float
    cover_area_ratio: float


@dataclass
class _PreparedCover:
    book: BookEntry
    shape: tuple[int, int]
    keypoints: list
    descriptors: np.ndarray


def _resize_for_features(image: np.ndarray, max_dimension: int = 1100) -> tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    scale = min(1.0, max_dimension / max(height, width, 1))
    if scale >= 1.0:
        return image, 1.0
    resized = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


class CoverMatcher:
    """Match a camera frame against locally registered covers using ORB + RANSAC."""

    def __init__(self, books_root: Path, config: dict):
        self.books_root = books_root
        self.min_good_matches = int(config.get("cover_min_good_matches", 16))
        self.min_inliers = int(config.get("cover_min_inliers", 10))
        self.min_inlier_ratio = float(config.get("cover_min_inlier_ratio", 0.38))
        self.min_area_ratio = float(config.get("cover_min_area_ratio", 0.06))
        self.ratio_test = float(config.get("cover_ratio_test", 0.75))
        self.orb = cv2.ORB_create(nfeatures=int(config.get("cover_orb_features", 2200)))
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.covers: list[_PreparedCover] = []
        self.reload()

    def reload(self) -> None:
        self.covers.clear()
        if not self.books_root.exists():
            return
        for metadata_path in sorted(self.books_root.glob("*/book.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                book_id = str(metadata["id"])
                title = str(metadata.get("title") or book_id)
                cover_path = metadata_path.parent / str(metadata.get("cover", "cover.jpg"))
                image = cv2.imread(str(cover_path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    print(f"Skipping book {book_id}: cannot read {cover_path}")
                    continue
                image, _ = _resize_for_features(image)
                keypoints, descriptors = self.orb.detectAndCompute(image, None)
                if descriptors is None or len(keypoints) < self.min_good_matches:
                    print(f"Skipping book {book_id}: cover has too few visual features")
                    continue
                self.covers.append(
                    _PreparedCover(
                        BookEntry(book_id, title, cover_path),
                        image.shape[:2],
                        keypoints,
                        descriptors,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                print(f"Skipping invalid book metadata {metadata_path}: {exc}")

    @property
    def entries(self) -> list[BookEntry]:
        return [cover.book for cover in self.covers]

    def match(self, frame: np.ndarray) -> CoverMatch | None:
        if not self.covers:
            return None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        gray, _ = _resize_for_features(gray)
        frame_keypoints, frame_descriptors = self.orb.detectAndCompute(gray, None)
        if frame_descriptors is None or len(frame_keypoints) < self.min_good_matches:
            return None

        frame_area = gray.shape[0] * gray.shape[1]
        best: CoverMatch | None = None
        for prepared in self.covers:
            pairs = self.matcher.knnMatch(prepared.descriptors, frame_descriptors, k=2)
            good = []
            for pair in pairs:
                if len(pair) < 2:
                    continue
                first, second = pair
                if first.distance < self.ratio_test * second.distance:
                    good.append(first)
            if len(good) < self.min_good_matches:
                continue

            source = np.float32([prepared.keypoints[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
            target = np.float32([frame_keypoints[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
            homography, mask = cv2.findHomography(source, target, cv2.RANSAC, 5.0)
            if homography is None or mask is None:
                continue
            inliers = int(mask.ravel().sum())
            inlier_ratio = inliers / len(good)
            if inliers < self.min_inliers or inlier_ratio < self.min_inlier_ratio:
                continue

            cover_height, cover_width = prepared.shape
            corners = np.float32(
                [[0, 0], [cover_width - 1, 0], [cover_width - 1, cover_height - 1], [0, cover_height - 1]]
            ).reshape(-1, 1, 2)
            projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
            if not np.isfinite(projected).all():
                continue
            if not cv2.isContourConvex(projected.astype(np.float32)):
                continue
            area_ratio = abs(float(cv2.contourArea(projected.astype(np.float32)))) / max(frame_area, 1)
            if area_ratio < self.min_area_ratio or area_ratio > 1.35:
                continue

            score = inliers + inlier_ratio * 20.0 + min(len(good), 100) * 0.05
            result = CoverMatch(prepared.book, score, len(good), inliers, inlier_ratio, area_ratio)
            if best is None or result.score > best.score:
                best = result
        return best


class BookConsensus:
    """Require repeated cover matches before changing the active book."""

    def __init__(self, confirmations: int = 2):
        self.confirmations = max(2, confirmations)
        self.confirmed: BookEntry | None = None
        self.pending: BookEntry | None = None
        self.pending_count = 0

    def scene_changed(self) -> None:
        self.pending = None
        self.pending_count = 0

    def observe(self, match: CoverMatch | None) -> tuple[bool, BookEntry | None]:
        if match is None:
            self.scene_changed()
            return False, self.confirmed
        book = match.book
        if self.confirmed and book.book_id == self.confirmed.book_id:
            self.scene_changed()
            return False, self.confirmed
        if self.pending and book.book_id == self.pending.book_id:
            self.pending_count += 1
        else:
            self.pending = book
            self.pending_count = 1
        if self.pending_count < self.confirmations:
            return False, self.confirmed
        self.confirmed = book
        self.scene_changed()
        return True, self.confirmed
