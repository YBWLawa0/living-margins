from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class ReadingStatePublisher:
    """Publish a small, stable contract for displays and other consumers."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = runtime_root
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.state_path = runtime_root / "state.json"
        self.events_path = runtime_root / "events.jsonl"
        self.feedback_path = runtime_root / "feedback.jsonl"
        self._lock = threading.RLock()
        self._state: dict[str, Any] | None = None
        self._revision = self._load_revision()

    def _load_revision(self) -> int:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return max(0, int(value.get("revision", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    @property
    def state(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._state) if self._state is not None else None

    def publish(
        self,
        *,
        book_id: str | None,
        title: str | None,
        pages: tuple[int, int] | list[int] | None,
        status: str,
        source: str,
        event: str,
        comment: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        page_list = None if pages is None else [int(page) for page in pages]
        if page_list is not None and len(page_list) != 2:
            raise ValueError("pages must contain exactly two page numbers")
        semantic = {
            "book_id": book_id,
            "title": title,
            "pages": page_list,
            "status": status,
            "comment": dict(comment) if comment is not None else None,
        }
        with self._lock:
            if self._state is not None and all(self._state.get(key) == value for key, value in semantic.items()):
                return False, dict(self._state)

            self._revision += 1
            state = {
                "schema_version": 1,
                "revision": self._revision,
                **semantic,
                "source": source,
                "event": event,
                "updated_at": _timestamp(),
            }
            self._write_snapshot(state)
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(state, ensure_ascii=False) + "\n")
            self._state = state
            return True, dict(state)

    def _write_snapshot(self, state: dict[str, Any]) -> None:
        temporary = self.runtime_root / f".state-{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def record_feedback(self, action: str, device_id: str = "unknown") -> dict[str, Any]:
        if action not in {"agree", "disagree"}:
            raise ValueError("action must be agree or disagree")
        with self._lock:
            state = self._state or {}
            record = {
                "timestamp": _timestamp(),
                "action": action,
                "device_id": str(device_id)[:80],
                "state_revision": state.get("revision"),
                "book_id": state.get("book_id"),
                "pages": state.get("pages"),
                "comment_id": (state.get("comment") or {}).get("id"),
            }
            with open(self.feedback_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record


class StateHttpServer:
    """Expose the current reading state using an ESP32-friendly HTTP API."""

    def __init__(self, publisher: ReadingStatePublisher, host: str = "0.0.0.0", port: int = 8765):
        self.publisher = publisher
        handler = self._make_handler(publisher)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="reading-state-http", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
        self._thread = None

    @staticmethod
    def _make_handler(publisher: ReadingStatePublisher):
        class Handler(BaseHTTPRequestHandler):
            server_version = "LibraryTerraState/1"

            def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
                payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                route = self.path.split("?", 1)[0]
                if route == "/state":
                    state = publisher.state
                    if state is None:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "state_not_ready"})
                    else:
                        self._send_json(HTTPStatus.OK, state)
                    return
                if route == "/health":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                route = self.path.split("?", 1)[0]
                if route != "/feedback":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 4096:
                        raise ValueError("invalid content length")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    record = publisher.record_feedback(str(body.get("action", "")), str(body.get("device_id", "unknown")))
                except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.CREATED, {"ok": True, "feedback": record})

            def do_OPTIONS(self) -> None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler
