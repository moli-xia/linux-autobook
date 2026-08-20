"""Administrator credentials, sessions and brute-force protection."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
PBKDF2_ITERATIONS = 600_000
USERNAME_RE = re.compile(r"[A-Za-z0-9_.@-]{3,40}")
MIN_PASSWORD_LENGTH = 8


class PasswordStore:
    """PBKDF2-hashed single administrator account stored as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.set_credentials("admin", "admin", initial=True)

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)

    def set_credentials(self, username: str, password: str, initial: bool = False) -> None:
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("用户名需为 3–40 位字母、数字、点、下划线、@ 或连字符")
        if not initial and len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"新密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
        salt = secrets.token_bytes(24)
        payload = {
            "username": username,
            "salt": salt.hex(),
            "password_hash": self._derive(password, salt).hex(),
            "must_change": bool(initial),
            "updated_at": int(time.time()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        with self._lock:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(payload, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            finally:
                Path(tmp).unlink(missing_ok=True)

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def authenticate(self, username: str, password: str) -> bool:
        try:
            state = self._read()
            salt = bytes.fromhex(state["salt"])
            expected = bytes.fromhex(state["password_hash"])
        except Exception:
            LOGGER.exception("读取管理账号状态失败")
            return False
        actual = self._derive(password, salt)
        name_ok = hmac.compare_digest(username.strip().encode(), str(state["username"]).encode())
        return name_ok and hmac.compare_digest(actual, expected)

    def username(self) -> str:
        try:
            return str(self._read().get("username") or "admin")
        except Exception:
            return "admin"

    def must_change(self) -> bool:
        try:
            return bool(self._read().get("must_change", False))
        except Exception:
            return False

    def updated_at(self) -> int:
        try:
            return int(self._read().get("updated_at", 0))
        except Exception:
            return 0


class SessionStore:
    def __init__(self, lifetime: int) -> None:
        self.lifetime = lifetime
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, username: str, address: str = "") -> dict[str, Any]:
        token = secrets.token_urlsafe(48)
        session = {
            "token": token,
            "username": username,
            "csrf": secrets.token_urlsafe(32),
            "created": time.time(),
            "expires": time.time() + self.lifetime,
            "address": address,
        }
        with self._lock:
            self._sessions[token] = session
        return dict(session)

    def get(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            for key in [key for key, value in self._sessions.items() if value["expires"] < now]:
                self._sessions.pop(key, None)
            session = self._sessions.get(token)
            if not session:
                return None
            session["expires"] = now + self.lifetime
            return dict(session)

    def delete(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for value in self._sessions.values() if value["expires"] >= now)


class LoginLimiter:
    """Sliding-window lockout keyed by client address."""

    def __init__(self, limit: int = 8, window: int = 600) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        cutoff = time.time() - self.window
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(address, []) if stamp > cutoff]
            self._attempts[address] = recent
            return len(recent) < self.limit

    def remaining_seconds(self, address: str) -> int:
        cutoff = time.time() - self.window
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(address, []) if stamp > cutoff]
            if len(recent) < self.limit:
                return 0
            return max(1, int(recent[0] + self.window - time.time()))

    def fail(self, address: str) -> None:
        with self._lock:
            self._attempts.setdefault(address, []).append(time.time())

    def clear(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)
