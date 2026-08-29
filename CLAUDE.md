# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

- `PROJECT_HANDOFF.md` — authoritative current status, per-file responsibilities, remaining tasks. Higher priority than `README.md` when they disagree.
- `DESIGN_RULES.md` — non-negotiable UI constraints for `web/` (white background, black text, strict rectangles, no shadows/gradients/rounded corners/decorative icons). Search CSS for non-zero `border-radius`, `box-shadow`, or `gradient` before committing frontend changes.
- `DEVELOPMENT_LOG.md` — rationale behind recognition-pipeline decisions; consult before altering vision, consensus, or motion logic.

## Commands

Everything runs on Windows against `.venv\Scripts\python.exe`. `run.bat` bootstraps the venv on first launch; other `.bat` files require it to have run at least once.

```powershell
# Full local link
.\run.bat                                       # camera OCR + :8765 state server
.\run_web.bat                                   # mobile web + API on :8780
.\.venv\Scripts\python.exe virtual_screen.py    # optional 480x320 device stand-in
.\.venv\Scripts\python.exe enroll_book.py       # capture + OCR-suggested title (add_book.bat wraps this)
.\.venv\Scripts\python.exe comment_editor.py    # local page-comment editor (add_comment.bat wraps this)
.\.venv\Scripts\python.exe manage_books.py list # inspect enrolled books

# Camera troubleshooting
.\run.ps1 --camera 1                            # try indexes 0/1/2 if MSMF/DSHOW fails

# Tests (Python + web JS)
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest tests.test_web_server -v   # single module
node --check web\app.js

# Firmware (PlatformIO, Waveshare ESP32-S3 4.3", COM7)
$env:PLATFORMIO_CORE_DIR='D:\p'                                   # short path avoids Windows MAX_PATH failures
$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
C:\Users\15160\.platformio\penv\Scripts\platformio.exe run -d firmware
C:\Users\15160\.platformio\penv\Scripts\platformio.exe run -d firmware --target upload --upload-port COM7

# OTA release (only after bumping firmware/include/firmware_version.h)
.\.venv\Scripts\python.exe firmware\publish_release.py
```

## Architecture

Three cooperating processes on the developer's Windows machine, connected by HTTP only:

```
Camera -> app.py (:8765 /state)  <-- HTTP polled by -->  living_margins_web.py (:8780)
                                                             |
                                            +----------------+----------------+
                                            v                                 v
                                    web/ (mobile browser)             firmware/ (ESP32-S3)
```

**`app.py` — recognition loop.** Runs camera + RapidOCR, orchestrates modules in `library_terra/`. Key invariant: no single OCR frame ever mutates confirmed state. Normal page changes require 2 consecutive identical observations (`confirmations_required`); jumps beyond `max_page_jump` require 3 (`large_jump_confirmations`). `PageConsensus` and `BookConsensus` enforce this — do not add code paths that write the confirmed page/book without going through them.

**`library_terra/` — recognition primitives.**
- `vision.py` — motion detection, sharpness gating, OCR candidate scoring by geometric position (bottom-outer page number heuristic).
- `books.py` — ORB + RANSAC cover matching against `books/*/cover.*`.
- `enrollment.py` — perspective-cropping and OCR-suggested title flow used by `enroll_book.py`.
- `comments.py` — hot-reloads `books/<id>/comments.json` roughly once per second when the current spread is stable.
- `reading_state.py` — the single source of truth for what leaves the process. `revision` only advances on semantic change; a plain OCR frame must not bump it. `state.json` in `runtime/` is a diagnostic snapshot, not a real-time feed — consumers must go through `:8765/state`.
- `telemetry.py` — session-scoped JSONL + keyframe capture under `debug/sessions/`.
- `web_database.py` — SQLite (`runtime/living_margins.db`) for users, device bindings, reading sessions, comment drafts/reviews, inspirations, feedback, and device tokens (stored as hashes only).

**`living_margins_web.py` — web/API server.** Serves `web/` and API v9 on `:8780`. Reads live vision state through `read_vision_state()` over HTTP; when that call fails, the API must return "recognition offline" rather than replaying `runtime/state.json`. Approved comments are written back to `books/<id>/comments.json` with stable IDs `web-comment-<id>` (idempotent — republishing the same comment is not a bug). ESP32s authenticate via a per-device token whose hash lives in the database; the raw token exists only in `firmware/include/device_secrets.h` (Git-ignored) and is rotated by re-issuing. The **first account created becomes admin** — this is a hackathon-only rule; do not assume it will survive to production.

**`firmware/`** — Arduino/LVGL on ESP32-S3. `src/main.cpp` handles Wi-Fi, QR pairing, live state polling, agree/disagree feedback, and OTA. OTA downloads to the alternate partition and only switches boot after size + SHA-256 match `runtime/firmware/release.json`. Binary size ceiling is `MAX_OTA_BINARY_SIZE = 0x640000` (~6.25 MB per partition).

**`web/`** — vanilla JS single-page app (`app.js` is a single file with a top-level `state` object; there is no framework). Must obey `DESIGN_RULES.md`.

## Data layout and what stays out of Git

`.gitignore` already blocks these; verify with `git status` before every commit:

- `runtime/` — SQLite database, state snapshot, event log, OTA release binaries, firmware backups. Contains user data and device tokens.
- `debug/` — camera captures and OCR diagnostics.
- `firmware/include/device_secrets.h` — Wi-Fi credentials, device token, server URL. Only `device_secrets.example.h` is tracked.
- `books/*/` — per-book covers and comments are treated as user content; only `books/README.md` is tracked.
- `.venv/`, `.pio/`, `.pio-core/`.

The user has shared Wi-Fi passwords in conversation before — never write them into any tracked file, including docs.

## Contract with the ESP32 and the mobile web

Both consume the same JSON schema documented in `README.md` under "ESP32 到货前的虚拟屏幕测试". When changing the state envelope:

- Bump `schema_version` only if the shape changes in a way old clients can't ignore.
- `revision` must monotonically increase and only advance on real semantic change; do not tie it to frame count or wall-clock ticks.
- `WEB_API_VERSION` in `living_margins_web.py` and `WEB_CAPABILITIES` gate what the frontend feature-detects; add capability strings rather than silently changing existing endpoints.

## Recognition-loop invariants to preserve

Regressions here are hard to catch in unit tests and expensive to debug on real hardware. Before editing `app.py` or `library_terra/vision.py`:

- Motion must be observed multiple times inside `motion_window_frames` before a turn event starts — single-frame thresholds cause thrashing on flicker.
- OCR runs on the full frame; the detected paper box is only a soft coordinate system for scoring candidate digits, never a crop.
- After a turn, the last confirmed page is retained until a new page passes consensus. Do not clear on OCR miss.
- `auto_reacquire_seconds` relaxes the *scoring bias toward the previous page* after ~3s stuck, but does NOT lower the 2-or-3-hit confirmation requirement.
