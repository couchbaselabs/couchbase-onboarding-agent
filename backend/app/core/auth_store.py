"""Admin password and browser sessions for the UI login.

The administrator sets the password on the first visit (there is no default
and no password in the environment to leak). It's stored as a salted scrypt
hash -- stdlib only, no new dependency -- in `auth.json` next to the migration
state file, mode 0600, written atomically like the state file itself.

Sessions are random 256-bit tokens handed to the browser after a successful
login and presented as `Authorization: Bearer <token>` (and as the websocket
subprotocol), exactly like API_AUTH_TOKEN. Only a SHA-256 of each token is
kept server-side, in memory and per process: a restart logs everyone out,
which matches the single-replica deployments this app targets.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)
audit = logging.getLogger("onboarding_agent.auth")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    b64 = base64.b64encode
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str) -> str | None:
    """A human-readable reason the password is unacceptable, or None."""
    s = get_settings()
    if len(password) < s.admin_password_min_length:
        return f"Use at least {s.admin_password_min_length} characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    if password.strip() != password:
        return "The password can't start or end with whitespace."
    if password.lower() in {"password", "administrator", "couchbase", "changeme", "password1234"}:
        return "That password is too common."
    return None


@dataclass
class Session:
    created: float
    last_seen: float

    def expired(self, now: float) -> bool:
        s = get_settings()
        return now - self.last_seen > s.session_idle_timeout_s or now - self.created > s.session_max_age_s


class AuthStore:
    _instance: AuthStore | None = None

    def __init__(self) -> None:
        self._hash: str | None = None
        self._password_set_at: str | None = None
        self._sessions: dict[str, Session] = {}
        self._load()

    @classmethod
    def instance(cls) -> AuthStore:
        if cls._instance is None:
            cls._instance = AuthStore()
        return cls._instance

    # ----------------------------------------------------------- password

    @staticmethod
    def path() -> Path:
        return Path(get_settings().migration_state_file).parent / "auth.json"

    def _load(self) -> None:
        p = self.path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            self._hash = raw.get("admin_password_hash") or None
            self._password_set_at = raw.get("password_set_at")
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not read %s: %s -- the admin password will have to be set again.", p, exc)

    def _persist(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".auth.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"admin_password_hash": self._hash, "password_set_at": self._password_set_at}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def login_enabled(self) -> bool:
        return get_settings().admin_login_enabled

    @property
    def password_set(self) -> bool:
        return bool(self._hash)

    @property
    def setup_required(self) -> bool:
        return self.login_enabled and not self.password_set

    @property
    def password_set_at(self) -> str | None:
        return self._password_set_at

    def set_password(self, password: str) -> None:
        self._hash = hash_password(password)
        self._password_set_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._persist()

    def check_password(self, password: str) -> bool:
        if not self._hash:
            return False
        return verify_password(password, self._hash)

    # ----------------------------------------------------------- sessions

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self) -> tuple[str, int]:
        """Returns (token, expires_at_epoch_seconds)."""
        self._sweep()
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[self._key(token)] = Session(created=now, last_seen=now)
        return token, int(now + get_settings().session_idle_timeout_s)

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        session = self._sessions.get(key)
        if session is None:
            return False
        now = time.time()
        if session.expired(now):
            self._sessions.pop(key, None)
            return False
        session.last_seen = now
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._key(token), None)

    def revoke_all_except(self, token: str | None) -> int:
        keep = self._key(token) if token else None
        before = len(self._sessions)
        self._sessions = {k: v for k, v in self._sessions.items() if k == keep}
        return before - len(self._sessions)

    def _sweep(self) -> None:
        now = time.time()
        for key in [k for k, v in self._sessions.items() if v.expired(now)]:
            self._sessions.pop(key, None)

    def reset_for_tests(self) -> None:
        self._hash = None
        self._password_set_at = None
        self._sessions.clear()
        p = self.path()
        if p.exists():
            p.unlink()
