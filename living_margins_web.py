from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import mimetypes
import re
import time
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from library_terra.comments import CommentStore
from library_terra.web_database import SESSION_TTL_SECONDS, WebDatabase


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
RUNTIME_ROOT = Path(os.environ.get("LM_RUNTIME_ROOT", str(PROJECT_ROOT / "runtime")))
BOOKS_ROOT = Path(os.environ.get("LM_BOOKS_ROOT", str(PROJECT_ROOT / "books")))
MOBILE_FRAME_ROOT = Path(
    os.environ.get("LM_MOBILE_FRAME_ROOT", str(RUNTIME_ROOT / "mobile_frames"))
)
COOKIE_NAME = "living_margins_session"
WEB_API_VERSION = 11
WEB_CAPABILITIES = [
    "inspirations",
    "comment_review",
    "user_feedback",
    "device_tokens",
    "qr_pairing",
    "device_state_gateway",
    "realtime_long_poll",
    "device_presence",
    "firmware_ota",
    "mobile_camera_ingest",
    "session_state_isolation",
]


def _vision_state_port() -> int:
    try:
        config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        return int(config.get("state_server_port", 8765))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 8765


VISION_STATE_HOST = "127.0.0.1"
VISION_STATE_PORT = _vision_state_port()


def read_vision_state() -> dict[str, Any] | None:
    connection: HTTPConnection | None = None
    try:
        connection = HTTPConnection(VISION_STATE_HOST, VISION_STATE_PORT, timeout=0.4)
        connection.request("GET", "/state", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != HTTPStatus.OK:
            response.read()
            return None
        payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            return None
        value = json.loads(payload.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, HTTPException, json.JSONDecodeError, UnicodeDecodeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def publish_approved_comment(
    comment: dict[str, Any], books_root: Path = BOOKS_ROOT
) -> dict[str, Any]:
    pages = comment.get("pages")
    if not isinstance(pages, list) or len(pages) != 2:
        raise ValueError("批注缺少有效页码")
    saved = CommentStore(books_root).upsert(
        str(comment.get("book_id") or ""),
        page=int(pages[0]),
        page_end=int(pages[1]),
        text=str(comment.get("body") or ""),
        author=str(comment.get("author_username") or "匿名读者"),
        priority=0,
        enabled=True,
        comment_id=f"web-comment-{int(comment['id'])}",
    )
    return saved.state_value()


FIRMWARE_RELEASE_ROOT = RUNTIME_ROOT / "firmware"
FIRMWARE_RELEASE_MANIFEST = FIRMWARE_RELEASE_ROOT / "release.json"
FIRMWARE_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MAX_OTA_BINARY_SIZE = 0x640000
VISION_SOURCE = os.environ.get("LM_VISION_SOURCE", "local").strip().lower()
RELAY_TOKEN = os.environ.get("LM_RELAY_TOKEN", "")
RELAY_MAX_AGE_SECONDS = float(os.environ.get("LM_RELAY_MAX_AGE_SECONDS", "15"))
RELAY_STATE_PATH = Path(
    os.environ.get("LM_RELAY_STATE_PATH", str(RUNTIME_ROOT / "relay_state.json"))
)
RELAY_STATE_ROOT = Path(
    os.environ.get("LM_RELAY_STATE_ROOT", str(RELAY_STATE_PATH.parent / "relay_states"))
)


def _version_tuple(value: str) -> tuple[int, int, int]:
    version = value.strip()
    if not FIRMWARE_VERSION_PATTERN.fullmatch(version):
        raise ValueError("固件版本号无效")
    return tuple(int(part) for part in version.split("."))


def read_firmware_release() -> dict[str, Any] | None:
    try:
        manifest = json.loads(FIRMWARE_RELEASE_MANIFEST.read_text(encoding="utf-8"))
        version = str(manifest["version"])
        _version_tuple(version)
        size = int(manifest["size"])
        expected_sha256 = str(manifest["sha256"]).lower()
        binary_name = str(manifest.get("binary") or "firmware.bin")
        binary_path = (FIRMWARE_RELEASE_ROOT / binary_name).resolve()
        if binary_path.parent != FIRMWARE_RELEASE_ROOT.resolve():
            return None
        if (
            size <= 0
            or size > MAX_OTA_BINARY_SIZE
            or len(expected_sha256) != 64
            or not binary_path.is_file()
            or binary_path.stat().st_size != size
        ):
            return None
        digest = hashlib.sha256()
        with binary_path.open("rb") as source:
            for chunk in iter(lambda: source.read(65_536), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            return None
        return {
            "version": version,
            "size": size,
            "sha256": expected_sha256,
            "path": binary_path,
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None

def relay_state_path(session_id: int | None = None) -> Path:
    return RELAY_STATE_PATH if session_id is None else RELAY_STATE_ROOT / f"session-{session_id}.json"


def read_relay_state(session_id: int | None = None) -> dict[str, Any] | None:
    try:
        payload = relay_state_path(session_id).read_bytes()
        if len(payload) > 1_048_576:
            return None
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            return None
        received_at = float(value.pop("_relay_received_at", 0))
        if received_at <= 0 or time.time() - received_at > RELAY_MAX_AGE_SECONDS:
            return None
        return value
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_relay_state(value: dict[str, Any]) -> dict[str, Any]:
    revision = value.get("revision")
    if not isinstance(revision, int) or revision < 0:
        raise ValueError("状态版本无效")
    status = str(value.get("status") or "")
    allowed = {"stable", "turning", "verifying", "unconfirmed", "recognizing", "stopped"}
    if status not in allowed:
        raise ValueError("阅读状态无效")
    current = read_relay_state()
    if current is not None and int(current.get("revision", -1)) > revision:
        raise ValueError("不能覆盖更新的阅读状态")
    RELAY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RELAY_STATE_PATH.with_suffix(".tmp")
    persisted = dict(value)
    persisted["_relay_received_at"] = time.time()
    temporary.write_text(json.dumps(persisted, ensure_ascii=False), encoding="utf-8")
    temporary.replace(RELAY_STATE_PATH)
    return value


def current_vision_state(
    user_id: int | None = None, database: WebDatabase | None = None
) -> dict[str, Any] | None:
    if VISION_SOURCE != "relay":
        return read_vision_state()
    if user_id is None or database is None:
        return read_relay_state()
    session = database.current_reading_session(user_id)
    if session is None:
        return None
    return read_relay_state(int(session["id"]))


def create_handler(database: WebDatabase):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LivingMarginsWeb/1"

        def _route(self) -> str:
            return urlsplit(self.path).path

        def _send_json(
            self,
            status: HTTPStatus,
            value: dict[str, Any],
            *,
            cookie: str | None = None,
            clear_cookie: bool = False,
        ) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}",
                )
            if clear_cookie:
                self.send_header(
                    "Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                )
            self.end_headers()
            self.wfile.write(payload)

        def _send_binary(self, release: dict[str, Any]) -> None:
            path = release["path"]
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(release["size"]))
            self.send_header("X-Firmware-Version", str(release["version"]))
            self.send_header("X-Firmware-SHA256", str(release["sha256"]))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(65_536), b""):
                    self.wfile.write(chunk)
        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"ok": False, "error": message})

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32_768:
                raise ValueError("请求内容无效")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("请求内容无效")
            return value

        def _token(self) -> str | None:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return None
            morsel = cookie.get(COOKIE_NAME)
            return None if morsel is None else morsel.value

        def _user(self) -> dict[str, Any] | None:
            return database.user_for_token(self._token())

        def _require_user(self) -> dict[str, Any] | None:
            user = self._user()
            if user is None:
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "请先登录")
            return user

        def do_GET(self) -> None:
            route = self._route()
            if route == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "api_version": WEB_API_VERSION,
                        "capabilities": WEB_CAPABILITIES,
                    },
                )
                return
            if route == "/api/devices":
                user = self._require_user()
                if user is None:
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "devices": database.devices_for_user(int(user["id"])),
                    },
                )
                return
            if route == "/api/bootstrap":
                user = self._require_user()
                if user is None:
                    return
                vision = current_vision_state(int(user["id"]), database)
                database.sync_reading_context(int(user["id"]), vision)
                review_queue = (
                    database.pending_comments_for_admin(int(user["id"]))
                    if user.get("role") == "admin"
                    else []
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "api_version": WEB_API_VERSION,
                        "capabilities": WEB_CAPABILITIES,
                        "user": user,
                        "devices": database.devices_for_user(int(user["id"])),
                        "reading_session": database.current_reading_session(int(user["id"])),
                        "comments": database.comments_for_user(int(user["id"])),
                        "inspirations": database.inspirations_for_user(int(user["id"])),
                        "review_queue": review_queue,
                        "vision": vision,
                    },
                )
                return
            if route == "/api/vision/state":
                user = self._require_user()
                if user is None:
                    return
                vision = current_vision_state(int(user["id"]), database)
                database.sync_reading_context(int(user["id"]), vision)
                self._send_json(HTTPStatus.OK, {"ok": True, "vision": vision})
                return
            if route.startswith("/api/"):
                self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            self._serve_static(route)

        def do_POST(self) -> None:
            route = self._route()
            if route == "/api/vision/frame":
                user = self._require_user()
                if user is None:
                    return
                session = database.current_reading_session(int(user["id"]))
                if session is None or session.get("status") != "active":
                    self._send_error_json(HTTPStatus.CONFLICT, "请先开始阅读")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if (
                    not self.headers.get("Content-Type", "").startswith("image/jpeg")
                    or length <= 0
                    or length > 1_500_000
                ):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "图片格式或大小无效")
                    return
                payload = self.rfile.read(length)
                if len(payload) != length or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "JPEG 图片无效")
                    return
                MOBILE_FRAME_ROOT.mkdir(parents=True, exist_ok=True)
                target = MOBILE_FRAME_ROOT / (
                    f"user-{int(user['id'])}-session-{int(session['id'])}.jpg"
                )
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(payload)
                temporary.replace(target)
                for stale in MOBILE_FRAME_ROOT.glob(f"user-{int(user['id'])}-session-*.jpg"):
                    if stale != target:
                        try:
                            stale.unlink()
                        except OSError:
                            pass
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {"ok": True, "bytes": length, "uploaded_at": time.time()},
                )
                return

            try:
                body = self._read_json()
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

            if route in {"/api/auth/register", "/api/auth/login"}:
                self._handle_auth(route, body)
                return
            if route == "/api/auth/logout":
                database.revoke_token(self._token())
                self._send_json(HTTPStatus.OK, {"ok": True}, clear_cookie=True)
                return

            if route == "/api/relay/state":
                supplied = self.headers.get("X-Relay-Token", "")
                if not RELAY_TOKEN or not hmac.compare_digest(supplied, RELAY_TOKEN):
                    self._send_error_json(HTTPStatus.UNAUTHORIZED, "上行代理认证失败")
                    return
                try:
                    state = write_relay_state(body)
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "revision": state["revision"]},
                    )
                except (OSError, ValueError, TypeError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

            if route in {
                "/api/device/firmware/check",
                "/api/device/firmware/download",
            }:
                try:
                    current_version = str(body.get("current_version", ""))
                    current_key = _version_tuple(current_version)
                    database.touch_device(
                        str(body.get("machine_code", "")),
                        str(body.get("device_token", "")),
                    )
                    release = read_firmware_release()
                    if route == "/api/device/firmware/check":
                        available = (
                            release is not None
                            and _version_tuple(str(release["version"])) > current_key
                        )
                        public_release = (
                            {
                                "version": release["version"],
                                "size": release["size"],
                                "sha256": release["sha256"],
                            }
                            if available
                            else None
                        )
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "ok": True,
                                "current_version": current_version,
                                "available": available,
                                "release": public_release,
                            },
                        )
                    elif release is None:
                        self._send_error_json(
                            HTTPStatus.NOT_FOUND, "服务器没有可用的固件包"
                        )
                    elif _version_tuple(str(release["version"])) <= current_key:
                        self._send_error_json(
                            HTTPStatus.CONFLICT, "当前已经是最新固件"
                        )
                    else:
                        self._send_binary(release)
                except PermissionError as exc:
                    self._send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
                except (ValueError, TypeError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if route == "/api/device/pairing/start":
                try:
                    pairing = database.start_device_pairing(
                        str(body.get("machine_code", "")),
                        str(body.get("device_token", "")),
                    )
                    self._send_json(HTTPStatus.CREATED, {"ok": True, "pairing": pairing})
                except PermissionError as exc:
                    self._send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
                except (ValueError, TypeError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if route == "/api/device/state":
                try:
                    raw_revision = body.get("revision")
                    revision = None if raw_revision is None else int(raw_revision)
                    wait_ms = int(body.get("wait_ms", 0))
                    if wait_ms < 0 or wait_ms > 10_000:
                        raise ValueError("状态等待时间无效")
                    device = database.touch_device(
                        str(body.get("machine_code", "")),
                        str(body.get("device_token", "")),
                        "realtime" if wait_ms > 0 else "polling",
                    )
                    owner_id = int(device["_paired_user_id"])
                    deadline = time.monotonic() + wait_ms / 1000
                    vision = current_vision_state(owner_id, database)
                    while (
                        wait_ms > 0
                        and isinstance(vision, dict)
                        and vision.get("revision") == revision
                        and time.monotonic() < deadline
                    ):
                        time.sleep(min(0.15, max(0.0, deadline - time.monotonic())))
                        vision = current_vision_state(owner_id, database)
                    changed = (
                        isinstance(vision, dict) and vision.get("revision") != revision
                    )
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "vision": vision, "changed": changed},
                    )
                except PermissionError as exc:
                    self._send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
                except (ValueError, TypeError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if route in {"/api/device/feedback", "/api/device/feedback/current"}:
                try:
                    machine_code = str(body.get("machine_code", ""))
                    device_token = str(body.get("device_token", ""))
                    comment_id = str(body.get("comment_id", ""))
                    if route == "/api/device/feedback/current":
                        feedback = database.feedback_for_device(machine_code, device_token, comment_id)
                        self._send_json(
                            HTTPStatus.OK, {"ok": True, "feedback": feedback}
                        )
                    else:
                        raw_page = body.get("page")
                        page = int(raw_page) if raw_page is not None else None
                        feedback, outcome = database.submit_device_feedback(
                            machine_code,
                            device_token,
                            comment_id,
                            str(body.get("action", "")),
                            book_id=str(body.get("book_id") or "") or None,
                            page=page,
                        )
                        self._send_json(
                            HTTPStatus.CREATED if outcome == "created" else HTTPStatus.OK,
                            {
                                "ok": True,
                                "feedback": feedback,
                                "outcome": outcome,
                            },
                        )
                except PermissionError as exc:
                    self._send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
                except (ValueError, TypeError) as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

            user = self._require_user()
            if user is None:
                return
            user_id = int(user["id"])
            try:
                if route == "/api/devices/pair/claim":
                    device = database.claim_device_pairing(
                        user_id, str(body.get("pairing_token", ""))
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "device": device})
                    return
                if route == "/api/devices/bind":
                    device = database.bind_device(user_id, str(body.get("machine_code", "")))
                    self._send_json(HTTPStatus.OK, {"ok": True, "device": device})
                    return
                if route == "/api/comments/draft":
                    raw_comment_id = body.get("comment_id")
                    comment_id = int(raw_comment_id) if raw_comment_id is not None else None
                    comment = database.save_comment_draft(
                        user_id, str(body.get("body", "")), comment_id
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "comment": comment})
                    return
                if route == "/api/comments/submit":
                    comment_id = int(body.get("comment_id"))
                    submitted_body = str(body["body"]) if "body" in body else None
                    comment = database.submit_comment(user_id, comment_id, submitted_body)
                    self._send_json(HTTPStatus.OK, {"ok": True, "comment": comment})
                    return
                if route == "/api/inspirations/mark":
                    vision = current_vision_state(user_id, database)
                    inspiration, created = database.mark_inspiration(user_id, vision)
                    self._send_json(
                        HTTPStatus.CREATED if created else HTTPStatus.OK,
                        {"ok": True, "inspiration": inspiration, "created": created},
                    )
                    return
                if route == "/api/inspirations/convert":
                    inspiration_id = int(body.get("inspiration_id"))
                    inspiration, comment = database.convert_inspiration_to_draft(
                        user_id, inspiration_id
                    )
                    self._send_json(
                        HTTPStatus.CREATED,
                        {
                            "ok": True,
                            "inspiration": inspiration,
                            "comment": comment,
                        },
                    )
                    return
                if route == "/api/admin/comments/review":
                    comment_id = int(body.get("comment_id"))
                    decision = str(body.get("decision", ""))
                    candidate = database.pending_comment_for_review(user_id, comment_id)
                    published = None
                    if decision == "approved":
                        published = publish_approved_comment(candidate)
                    comment = database.review_comment(user_id, comment_id, decision)
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "comment": comment, "published": published},
                    )
                    return
                if route == "/api/reading/start":
                    raw_device_id = body.get("device_id")
                    device_id = int(raw_device_id) if raw_device_id is not None else None
                    session = database.start_reading_session(
                        user_id, device_id, (
                            read_vision_state() if VISION_SOURCE != "relay" else None
                        )
                    )
                    self._send_json(HTTPStatus.CREATED, {"ok": True, "reading_session": session})
                    return
                if route in {
                    "/api/reading/pause",
                    "/api/reading/resume",
                    "/api/reading/end",
                }:
                    if route == "/api/reading/pause":
                        vision = current_vision_state(user_id, database)
                        if not vision or not vision.get("book_id") or not vision.get("pages"):
                            raise ValueError("识别服务尚未确认书籍和页码，暂时不能写批注")
                        database.sync_reading_context(user_id, vision)
                    status = {
                        "/api/reading/pause": "paused",
                        "/api/reading/resume": "active",
                        "/api/reading/end": "ended",
                    }[route]
                    session = database.set_reading_status(user_id, status)
                    self._send_json(HTTPStatus.OK, {"ok": True, "reading_session": session})
                    return
            except (ValueError, TypeError) as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")

        def _handle_auth(self, route: str, body: dict[str, Any]) -> None:
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))
            try:
                if route == "/api/auth/register":
                    user = database.create_user(username, password)
                else:
                    user = database.authenticate(username, password)
                    if user is None:
                        self._send_error_json(HTTPStatus.UNAUTHORIZED, "用户名或密码错误")
                        return
                token = database.create_auth_session(int(user["id"]))
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "user": user}, cookie=token)

        def _serve_static(self, route: str) -> None:
            relative = "index.html" if route in {"", "/"} else route.lstrip("/")
            candidate = (WEB_ROOT / relative).resolve()
            try:
                candidate.relative_to(WEB_ROOT.resolve())
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.is_file():
                candidate = WEB_ROOT / "index.html"
            try:
                payload = candidate.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Living Margins mobile web app")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--database", type=Path, default=RUNTIME_ROOT / "living_margins.db")
    args = parser.parse_args()

    database = WebDatabase(args.database)
    server = ThreadingHTTPServer((args.host, args.port), create_handler(database))
    server.daemon_threads = True
    print(f"Living Margins web: http://127.0.0.1:{args.port}")
    print("Demo screen machine code: LM-DEMO-0001")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
