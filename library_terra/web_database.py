from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
DEMO_DEVICE_CODE = "LM-DEMO-0001"


class WebDatabase:
    """Persistent users, screen bindings, and reading sessions for the mobile web app."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'reader'
                        CHECK(role IN ('reader', 'admin')),
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique
                    ON users(username);

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS auth_sessions_token_unique
                    ON auth_sessions(token_hash);
                CREATE INDEX IF NOT EXISTS auth_sessions_user
                    ON auth_sessions(user_id);

                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    machine_code TEXT NOT NULL COLLATE NOCASE,
                    name TEXT NOT NULL,
                    paired_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    last_seen_at REAL,
                    connection_mode TEXT,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS devices_machine_code_unique
                    ON devices(machine_code);
                CREATE INDEX IF NOT EXISTS devices_paired_user
                    ON devices(paired_user_id);

                CREATE TABLE IF NOT EXISTS device_pairing_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    claimed_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS device_pairing_token_unique
                    ON device_pairing_sessions(token_hash);
                CREATE INDEX IF NOT EXISTS device_pairing_expiry
                    ON device_pairing_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS comment_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                    comment_id TEXT NOT NULL,
                    book_id TEXT,
                    page INTEGER,
                    action TEXT NOT NULL CHECK(action IN ('agree', 'disagree')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(user_id, comment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_comment_feedback_comment_action
                    ON comment_feedback(comment_id, action);

                CREATE TABLE IF NOT EXISTS reading_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'ended')),
                    book_id TEXT,
                    book_title TEXT,
                    pages_json TEXT,
                    state_revision INTEGER,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE INDEX IF NOT EXISTS reading_sessions_user_status
                    ON reading_sessions(user_id, status);

                CREATE TABLE IF NOT EXISTS inspirations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    reading_session_id INTEGER REFERENCES reading_sessions(id) ON DELETE SET NULL,
                    book_id TEXT NOT NULL,
                    book_title TEXT,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    state_revision INTEGER,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open', 'converted')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inspirations_user_status_created
                    ON inspirations(user_id, status, created_at DESC, id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_inspirations_open_page
                    ON inspirations(user_id, book_id, page_start, page_end)
                    WHERE status = 'open';

                CREATE TABLE IF NOT EXISTS web_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    reading_session_id INTEGER REFERENCES reading_sessions(id) ON DELETE SET NULL,
                    inspiration_id INTEGER REFERENCES inspirations(id) ON DELETE SET NULL,
                    book_id TEXT NOT NULL,
                    book_title TEXT,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL
                        CHECK(status IN ('draft', 'pending', 'approved', 'rejected')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    submitted_at REAL,
                    reviewed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_web_comments_user_updated
                    ON web_comments(user_id, updated_at DESC, id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_web_comments_session_open_draft
                    ON web_comments(reading_session_id)
                    WHERE status = 'draft' AND reading_session_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_web_comments_pending_submitted
                    ON web_comments(submitted_at ASC, id ASC)
                    WHERE status = 'pending';
                """
            )
            user_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(users)")
            }
            if "role" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'reader'"
                )
            comment_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(web_comments)")
            }
            device_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(devices)")
            }
            if "token_hash" not in device_columns:
                connection.execute("ALTER TABLE devices ADD COLUMN token_hash TEXT")
            if "connection_mode" not in device_columns:
                connection.execute("ALTER TABLE devices ADD COLUMN connection_mode TEXT")
            if "inspiration_id" not in comment_columns:
                connection.execute(
                    "ALTER TABLE web_comments ADD COLUMN inspiration_id INTEGER"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_web_comments_inspiration
                ON web_comments(inspiration_id)
                WHERE inspiration_id IS NOT NULL
                """
            )
            has_admin = connection.execute(
                "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
            ).fetchone()
            if has_admin is None:
                connection.execute(
                    """
                    UPDATE users SET role = 'admin'
                    WHERE id = (SELECT MIN(id) FROM users)
                    """
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO devices(machine_code, name, created_at)
                VALUES (?, ?, ?)
                """,
                (DEMO_DEVICE_CODE, "虚拟 ESP32 屏幕", time.time()),
            )
            connection.execute(
                "DROP INDEX IF EXISTS idx_web_comments_user_status_updated"
            )
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> str:
        value = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return value.hex()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "role": str(row["role"]),
        }

    def create_user(self, username: str, password: str) -> dict[str, Any]:
        username = username.strip()
        if len(username) < 2 or len(username) > 24:
            raise ValueError("用户名需要 2–24 个字符")
        if len(password) < 6 or len(password) > 128:
            raise ValueError("密码至少需要 6 个字符")
        salt = secrets.token_bytes(16)
        try:
            with self._connection() as connection:
                role = (
                    "admin"
                    if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
                    else "reader"
                )
                cursor = connection.execute(
                    """
                    INSERT INTO users(username, password_hash, password_salt, role, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        self._password_hash(password, salt),
                        salt.hex(),
                        role,
                        time.time(),
                    ),
                )
                row = connection.execute(
                    "SELECT id, username, role FROM users WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("这个用户名已经存在") from exc
        assert row is not None
        return self._public_user(row)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
        if row is None:
            return None
        actual = self._password_hash(password, bytes.fromhex(str(row["password_salt"])))
        if not hmac.compare_digest(actual, str(row["password_hash"])):
            return None
        return self._public_user(row)

    def create_auth_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                """
                INSERT INTO auth_sessions(token_hash, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (self._token_hash(token), user_id, now + SESSION_TTL_SECONDS, now),
            )
        return token

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, users.role
                FROM auth_sessions
                JOIN users ON users.id = auth_sessions.user_id
                WHERE auth_sessions.token_hash = ? AND auth_sessions.expires_at > ?
                """,
                (self._token_hash(token), now),
            ).fetchone()
        return None if row is None else self._public_user(row)

    def revoke_token(self, token: str | None) -> None:
        if not token:
            return
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (self._token_hash(token),)
            )

    def bind_device(self, user_id: int, machine_code: str) -> dict[str, Any]:
        code = machine_code.strip().upper()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE machine_code = ? COLLATE NOCASE", (code,)
            ).fetchone()
            if row is None:
                raise ValueError("没有找到这台屏幕，请检查机器码")
            owner = row["paired_user_id"]
            if owner is not None and int(owner) != user_id:
                raise ValueError("这台屏幕已经被其他用户绑定")
            connection.execute(
                "UPDATE devices SET paired_user_id = ?, last_seen_at = ? WHERE id = ?",
                (user_id, time.time(), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (row["id"],)
            ).fetchone()
        assert updated is not None
        return self._public_device(updated)

    def rotate_device_token(self, machine_code: str) -> str:
        """Issue a new device credential and invalidate the previous one."""
        code = machine_code.strip().upper()
        token = secrets.token_urlsafe(32)
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE devices SET token_hash = ? WHERE machine_code = ? COLLATE NOCASE",
                (self._token_hash(token), code),
            )
            if cursor.rowcount != 1:
                raise ValueError("没有找到这台屏幕")
        return token

    @staticmethod
    def _public_device(row: sqlite3.Row) -> dict[str, Any]:
        last_seen_at = row["last_seen_at"]
        online = last_seen_at is not None and time.time() - float(last_seen_at) <= 20
        return {
            "id": int(row["id"]),
            "machine_code": str(row["machine_code"]),
            "name": str(row["name"]),
            "last_seen_at": last_seen_at,
            "online": online,
            "connection_mode": row["connection_mode"] if online else None,
        }

    def devices_for_user(self, user_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM devices WHERE paired_user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
        return [self._public_device(row) for row in rows]

    @staticmethod
    def _public_feedback(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "device_id": row["device_id"],
            "comment_id": str(row["comment_id"]),
            "book_id": row["book_id"],
            "page": row["page"],
            "action": str(row["action"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def feedback_for_user(self, user_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM comment_feedback
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._public_feedback(row) for row in rows]

    def start_device_pairing(
        self, machine_code: str, device_token: str, ttl_seconds: int = 300
    ) -> dict[str, Any]:
        if ttl_seconds < 30 or ttl_seconds > 900:
            raise ValueError("配对有效期无效")
        code = machine_code.strip().upper()
        now = time.time()
        claim_token = secrets.token_urlsafe(24)
        with self._connection() as connection:
            device = connection.execute(
                "SELECT * FROM devices WHERE machine_code = ? COLLATE NOCASE", (code,)
            ).fetchone()
            if device is None:
                raise ValueError("没有找到这台屏幕")
            expected = str(device["token_hash"] or "")
            actual = self._token_hash(device_token) if device_token else ""
            if not expected or not hmac.compare_digest(actual, expected):
                raise PermissionError("设备令牌无效")
            connection.execute(
                "DELETE FROM device_pairing_sessions WHERE device_id = ? OR expires_at <= ?",
                (device["id"], now),
            )
            connection.execute(
                """
                INSERT INTO device_pairing_sessions(
                    device_id, token_hash, expires_at, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (device["id"], self._token_hash(claim_token), now + ttl_seconds, now),
            )
        return {"pairing_token": claim_token, "expires_at": now + ttl_seconds}

    def claim_device_pairing(self, user_id: int, pairing_token: str) -> dict[str, Any]:
        token = pairing_token.strip()
        if not token:
            raise ValueError("配对链接无效")
        now = time.time()
        with self._connection() as connection:
            pairing = connection.execute(
                """
                SELECT device_pairing_sessions.*, devices.paired_user_id
                FROM device_pairing_sessions
                JOIN devices ON devices.id = device_pairing_sessions.device_id
                WHERE device_pairing_sessions.token_hash = ?
                """,
                (self._token_hash(token),),
            ).fetchone()
            if pairing is None or pairing["expires_at"] <= now:
                raise ValueError("配对链接已失效，请在屏幕上重新生成")
            if pairing["claimed_at"] is not None:
                raise ValueError("配对链接已经使用")
            owner = pairing["paired_user_id"]
            if owner is not None and int(owner) != user_id:
                raise ValueError("这台屏幕已经被其他用户绑定")
            connection.execute(
                "UPDATE devices SET paired_user_id = ?, last_seen_at = ? WHERE id = ?",
                (user_id, now, pairing["device_id"]),
            )
            connection.execute(
                "UPDATE device_pairing_sessions SET claimed_at = ? WHERE id = ?",
                (now, pairing["id"]),
            )
            device = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (pairing["device_id"],)
            ).fetchone()
        assert device is not None
        return self._public_device(device)
    @staticmethod
    def _paired_device(
        connection: sqlite3.Connection, machine_code: str, device_token: str
    ) -> sqlite3.Row:
        code = machine_code.strip().upper()
        row = connection.execute(
            "SELECT * FROM devices WHERE machine_code = ? COLLATE NOCASE", (code,)
        ).fetchone()
        if row is None:
            raise ValueError("没有找到这台屏幕")
        if row["paired_user_id"] is None:
            raise ValueError("这台屏幕尚未绑定用户")
        expected = str(row["token_hash"] or "")
        actual = WebDatabase._token_hash(device_token) if device_token else ""
        if not expected or not hmac.compare_digest(actual, expected):
            raise PermissionError("设备令牌无效")
        return row

    def feedback_for_device(
        self, machine_code: str, device_token: str, comment_id: str
    ) -> dict[str, Any] | None:
        target_comment_id = comment_id.strip()
        if not target_comment_id:
            raise ValueError("批注编号不能为空")
        with self._connection() as connection:
            device = self._paired_device(connection, machine_code, device_token)
            row = connection.execute(
                """
                SELECT * FROM comment_feedback
                WHERE user_id = ? AND comment_id = ?
                """,
                (device["paired_user_id"], target_comment_id),
            ).fetchone()
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (time.time(), device["id"]),
            )
        return None if row is None else self._public_feedback(row)

    def touch_device(
        self, machine_code: str, device_token: str, connection_mode: str | None = None
    ) -> dict[str, Any]:
        """Authenticate a paired device and update its online heartbeat."""
        if connection_mode is not None and connection_mode not in {"realtime", "polling"}:
            raise ValueError("设备连接模式无效")
        now = time.time()
        with self._connection() as connection:
            device = self._paired_device(connection, machine_code, device_token)
            if connection_mode is None:
                connection.execute(
                    "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                    (now, device["id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE devices
                    SET last_seen_at = ?, connection_mode = ?
                    WHERE id = ?
                    """,
                    (now, connection_mode, device["id"]),
                )
            updated = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (device["id"],)
            ).fetchone()
        assert updated is not None
        result = self._public_device(updated)
        result["_paired_user_id"] = int(updated["paired_user_id"])
        return result
    def submit_device_feedback(
        self,
        machine_code: str,
        device_token: str,
        comment_id: str,
        action: str,
        *,
        book_id: str | None = None,
        page: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        target_comment_id = comment_id.strip()
        if not target_comment_id or len(target_comment_id) > 128:
            raise ValueError("批注编号无效")
        if action not in {"agree", "disagree"}:
            raise ValueError("反馈只能是赞同或不赞同")
        safe_page = None if page is None else int(page)
        if safe_page is not None and safe_page <= 0:
            raise ValueError("反馈页码无效")
        now = time.time()
        with self._connection() as connection:
            device = self._paired_device(connection, machine_code, device_token)
            user_id = int(device["paired_user_id"])
            existing = connection.execute(
                """
                SELECT * FROM comment_feedback
                WHERE user_id = ? AND comment_id = ?
                """,
                (user_id, target_comment_id),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO comment_feedback(
                        user_id, device_id, comment_id, book_id, page,
                        action, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        device["id"],
                        target_comment_id,
                        book_id,
                        safe_page,
                        action,
                        now,
                        now,
                    ),
                )
                outcome = "created"
                feedback_id = cursor.lastrowid
            elif existing["action"] == action:
                outcome = "unchanged"
                feedback_id = existing["id"]
            else:
                connection.execute(
                    """
                    UPDATE comment_feedback
                    SET device_id = ?, book_id = ?, page = ?, action = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        device["id"],
                        book_id,
                        safe_page,
                        action,
                        now,
                        existing["id"],
                    ),
                )
                outcome = "changed"
                feedback_id = existing["id"]
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (now, device["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM comment_feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        assert updated is not None
        return self._public_feedback(updated), outcome

    def submit_user_feedback(
        self,
        user_id: int,
        comment_id: str,
        action: str,
        *,
        book_id: str | None = None,
        page: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        target_comment_id = comment_id.strip()
        if not target_comment_id or len(target_comment_id) > 128:
            raise ValueError("批注编号无效")
        if action not in {"agree", "disagree"}:
            raise ValueError("反馈只能是赞同或不赞同")
        safe_page = None if page is None else int(page)
        if safe_page is not None and safe_page <= 0:
            raise ValueError("反馈页码无效")
        now = time.time()
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM comment_feedback
                WHERE user_id = ? AND comment_id = ?
                """,
                (user_id, target_comment_id),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO comment_feedback(
                        user_id, device_id, comment_id, book_id, page,
                        action, created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        target_comment_id,
                        book_id,
                        safe_page,
                        action,
                        now,
                        now,
                    ),
                )
                outcome = "created"
                feedback_id = cursor.lastrowid
            elif existing["action"] == action:
                outcome = "unchanged"
                feedback_id = existing["id"]
            else:
                connection.execute(
                    """
                    UPDATE comment_feedback
                    SET book_id = ?, page = ?, action = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (book_id, safe_page, action, now, existing["id"]),
                )
                outcome = "changed"
                feedback_id = existing["id"]
            updated = connection.execute(
                "SELECT * FROM comment_feedback WHERE id = ?", (feedback_id,)
            ).fetchone()
        assert updated is not None
        return self._public_feedback(updated), outcome

    @staticmethod
    def _public_reading_session(row: sqlite3.Row) -> dict[str, Any]:
        try:
            pages = json.loads(row["pages_json"]) if row["pages_json"] else None
        except json.JSONDecodeError:
            pages = None
        return {
            "id": int(row["id"]),
            "device_id": row["device_id"],
            "status": str(row["status"]),
            "book_id": row["book_id"],
            "book_title": row["book_title"],
            "pages": pages,
            "state_revision": row["state_revision"],
            "started_at": float(row["started_at"]),
            "updated_at": float(row["updated_at"]),
            "ended_at": row["ended_at"],
        }

    def current_reading_session(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM reading_sessions
                WHERE user_id = ? AND status IN ('active', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return None if row is None else self._public_reading_session(row)

    def start_reading_session(
        self, user_id: int, device_id: int | None, vision_state: dict[str, Any] | None
    ) -> dict[str, Any]:
        if device_id is not None:
            owned = {item["id"] for item in self.devices_for_user(user_id)}
            if device_id not in owned:
                raise ValueError("请先绑定这台屏幕")
        now = time.time()
        state = vision_state or {}
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE reading_sessions SET status = 'ended', updated_at = ?, ended_at = ?
                WHERE user_id = ? AND status IN ('active', 'paused')
                """,
                (now, now, user_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO reading_sessions(
                    user_id, device_id, status, book_id, book_title, pages_json,
                    state_revision, started_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    device_id,
                    state.get("book_id"),
                    state.get("title"),
                    json.dumps(state.get("pages")) if state.get("pages") else None,
                    state.get("revision"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM reading_sessions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._public_reading_session(row)

    def set_reading_status(self, user_id: int, status: str) -> dict[str, Any]:
        if status not in {"active", "paused", "ended"}:
            raise ValueError("无效的阅读状态")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM reading_sessions
                WHERE user_id = ? AND status IN ('active', 'paused')
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("当前没有进行中的阅读")
            now = time.time()
            ended_at = now if status == "ended" else None
            connection.execute(
                """
                UPDATE reading_sessions
                SET status = ?, updated_at = ?, ended_at = ? WHERE id = ?
                """,
                (status, now, ended_at, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM reading_sessions WHERE id = ?", (row["id"],)
            ).fetchone()
        assert updated is not None
        return self._public_reading_session(updated)

    def sync_reading_context(self, user_id: int, vision_state: dict[str, Any] | None) -> None:
        if not vision_state:
            return
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM reading_sessions
                WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE reading_sessions
                SET book_id = ?, book_title = ?, pages_json = ?, state_revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    vision_state.get("book_id"),
                    vision_state.get("title"),
                    json.dumps(vision_state.get("pages")) if vision_state.get("pages") else None,
                    vision_state.get("revision"),
                    time.time(),
                    row["id"],
                ),
            )

    @staticmethod
    def _public_comment(row: sqlite3.Row) -> dict[str, Any]:
        value = {
            "id": int(row["id"]),
            "reading_session_id": row["reading_session_id"],
            "inspiration_id": row["inspiration_id"],
            "book_id": str(row["book_id"]),
            "book_title": row["book_title"],
            "pages": [int(row["page_start"]), int(row["page_end"])],
            "body": str(row["body"]),
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "submitted_at": row["submitted_at"],
            "reviewed_at": row["reviewed_at"],
        }
        if "author_username" in row.keys():
            value["author_username"] = str(row["author_username"])
        return value

    @staticmethod
    def _public_inspiration(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "reading_session_id": row["reading_session_id"],
            "book_id": str(row["book_id"]),
            "book_title": row["book_title"],
            "pages": [int(row["page_start"]), int(row["page_end"])],
            "state_revision": row["state_revision"],
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def inspirations_for_user(
        self, user_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM inspirations
                WHERE user_id = ? AND status = 'open'
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [self._public_inspiration(row) for row in rows]

    def mark_inspiration(
        self, user_id: int, vision_state: dict[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        state = vision_state or {}
        book_id = str(state.get("book_id") or "").strip()
        pages = state.get("pages")
        if not book_id or not isinstance(pages, list) or len(pages) != 2:
            raise ValueError("当前还没有可信的书籍和页码")
        page_start, page_end = int(pages[0]), int(pages[1])
        if page_start <= 0 or page_end < page_start:
            raise ValueError("当前页码无效")
        now = time.time()
        with self._connection() as connection:
            session = connection.execute(
                """
                SELECT * FROM reading_sessions
                WHERE user_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if session is None:
                raise ValueError("请先开始阅读")
            existing = connection.execute(
                """
                SELECT * FROM inspirations
                WHERE user_id = ? AND book_id = ? AND page_start = ?
                    AND page_end = ? AND status = 'open'
                """,
                (user_id, book_id, page_start, page_end),
            ).fetchone()
            if existing is not None:
                return self._public_inspiration(existing), False
            cursor = connection.execute(
                """
                INSERT INTO inspirations(
                    user_id, reading_session_id, book_id, book_title,
                    page_start, page_end, state_revision, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    user_id,
                    session["id"],
                    book_id,
                    state.get("title"),
                    page_start,
                    page_end,
                    state.get("revision"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM inspirations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._public_inspiration(row), True

    def convert_inspiration_to_draft(
        self, user_id: int, inspiration_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.time()
        with self._connection() as connection:
            inspiration = connection.execute(
                "SELECT * FROM inspirations WHERE id = ? AND user_id = ?",
                (inspiration_id, user_id),
            ).fetchone()
            if inspiration is None:
                raise ValueError("没有找到这条灵感标记")
            existing = connection.execute(
                "SELECT * FROM web_comments WHERE inspiration_id = ? AND user_id = ?",
                (inspiration_id, user_id),
            ).fetchone()
            if existing is None:
                if inspiration["status"] != "open":
                    raise ValueError("这条灵感已经转为批注")
                cursor = connection.execute(
                    """
                    INSERT INTO web_comments(
                        user_id, reading_session_id, inspiration_id, book_id, book_title,
                        page_start, page_end, body, status, created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, '', 'draft', ?, ?)
                    """,
                    (
                        user_id,
                        inspiration_id,
                        inspiration["book_id"],
                        inspiration["book_title"],
                        inspiration["page_start"],
                        inspiration["page_end"],
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM web_comments WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                connection.execute(
                    """
                    UPDATE inspirations SET status = 'converted', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, inspiration_id),
                )
            updated_inspiration = connection.execute(
                "SELECT * FROM inspirations WHERE id = ?", (inspiration_id,)
            ).fetchone()
        assert existing is not None and updated_inspiration is not None
        return self._public_inspiration(updated_inspiration), self._public_comment(existing)

    @staticmethod
    def _require_admin(connection: sqlite3.Connection, user_id: int) -> None:
        row = connection.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or row["role"] != "admin":
            raise ValueError("只有管理员可以审核批注")

    def pending_comments_for_admin(
        self, admin_user_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as connection:
            self._require_admin(connection, admin_user_id)
            rows = connection.execute(
                """
                SELECT web_comments.*, users.username AS author_username
                FROM web_comments
                JOIN users ON users.id = web_comments.user_id
                WHERE web_comments.status = 'pending'
                ORDER BY web_comments.submitted_at ASC, web_comments.id ASC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [self._public_comment(row) for row in rows]

    def pending_comment_for_review(
        self, admin_user_id: int, comment_id: int
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._require_admin(connection, admin_user_id)
            row = connection.execute(
                """
                SELECT web_comments.*, users.username AS author_username
                FROM web_comments
                JOIN users ON users.id = web_comments.user_id
                WHERE web_comments.id = ? AND web_comments.status = 'pending'
                """,
                (comment_id,),
            ).fetchone()
        if row is None:
            raise ValueError("这条批注不存在或已经审核")
        return self._public_comment(row)

    def review_comment(
        self, admin_user_id: int, comment_id: int, decision: str
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("无效的审核结果")
        with self._connection() as connection:
            self._require_admin(connection, admin_user_id)
            row = connection.execute(
                "SELECT status FROM web_comments WHERE id = ?", (comment_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("这条批注不存在或已经审核")
            now = time.time()
            connection.execute(
                """
                UPDATE web_comments
                SET status = ?, updated_at = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (decision, now, now, comment_id),
            )
            updated = connection.execute(
                """
                SELECT web_comments.*, users.username AS author_username
                FROM web_comments
                JOIN users ON users.id = web_comments.user_id
                WHERE web_comments.id = ?
                """,
                (comment_id,),
            ).fetchone()
        assert updated is not None
        return self._public_comment(updated)

    def comments_for_user(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM web_comments
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [self._public_comment(row) for row in rows]

    def comment_for_user(self, user_id: int, comment_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_comments WHERE id = ? AND user_id = ?",
                (comment_id, user_id),
            ).fetchone()
        return None if row is None else self._public_comment(row)

    @staticmethod
    def _validated_draft_body(body: str) -> str:
        value = chr(10).join(str(body).splitlines())
        if len(value) > 2000:
            raise ValueError("批注不能超过 2000 个字符")
        return value

    def save_comment_draft(
        self, user_id: int, body: str, comment_id: int | None = None
    ) -> dict[str, Any]:
        value = self._validated_draft_body(body)
        now = time.time()
        with self._connection() as connection:
            if comment_id is not None:
                row = connection.execute(
                    "SELECT * FROM web_comments WHERE id = ? AND user_id = ?",
                    (comment_id, user_id),
                ).fetchone()
                if row is None:
                    raise ValueError("没有找到这条批注")
                if row["status"] != "draft":
                    raise ValueError("只有草稿可以继续修改")
                connection.execute(
                    "UPDATE web_comments SET body = ?, updated_at = ? WHERE id = ?",
                    (value, now, comment_id),
                )
            else:
                session = connection.execute(
                    """
                    SELECT * FROM reading_sessions
                    WHERE user_id = ? AND status = 'paused'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
                if session is None:
                    raise ValueError("请先暂停当前阅读再写批注")
                if not session["book_id"]:
                    raise ValueError("当前还没有识别到书籍")
                try:
                    pages = json.loads(session["pages_json"]) if session["pages_json"] else None
                except json.JSONDecodeError:
                    pages = None
                if not isinstance(pages, list) or len(pages) != 2:
                    raise ValueError("当前还没有可信页码")
                page_start, page_end = int(pages[0]), int(pages[1])
                if page_start <= 0 or page_end < page_start:
                    raise ValueError("当前页码无效")
                row = connection.execute(
                    """
                    SELECT * FROM web_comments
                    WHERE reading_session_id = ? AND user_id = ? AND status = 'draft'
                    """,
                    (session["id"], user_id),
                ).fetchone()
                if row is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO web_comments(
                            user_id, reading_session_id, book_id, book_title,
                            page_start, page_end, body, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                        """,
                        (
                            user_id,
                            session["id"],
                            session["book_id"],
                            session["book_title"],
                            page_start,
                            page_end,
                            value,
                            now,
                            now,
                        ),
                    )
                    comment_id = int(cursor.lastrowid)
                else:
                    comment_id = int(row["id"])
                    connection.execute(
                        "UPDATE web_comments SET body = ?, updated_at = ? WHERE id = ?",
                        (value, now, comment_id),
                    )

            updated = connection.execute(
                "SELECT * FROM web_comments WHERE id = ? AND user_id = ?",
                (comment_id, user_id),
            ).fetchone()
        assert updated is not None
        return self._public_comment(updated)

    def submit_comment(
        self, user_id: int, comment_id: int, body: str | None = None
    ) -> dict[str, Any]:
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM web_comments WHERE id = ? AND user_id = ?",
                (comment_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("没有找到这条批注")
            if row["status"] != "draft":
                raise ValueError("这条批注已经提交，不能重复提交")
            value = str(row["body"]) if body is None else self._validated_draft_body(body)
            if len(value.strip()) < 2:
                raise ValueError("请至少写下两个字符再提交")
            connection.execute(
                """
                UPDATE web_comments
                SET body = ?, status = 'pending', updated_at = ?, submitted_at = ?
                WHERE id = ?
                """,
                (value, now, now, comment_id),
            )
            updated = connection.execute(
                "SELECT * FROM web_comments WHERE id = ?", (comment_id,)
            ).fetchone()
        assert updated is not None
        return self._public_comment(updated)
