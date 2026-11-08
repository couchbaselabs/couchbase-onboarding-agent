"""Users, roles and browser sessions for the UI login.

The administrator (`admin`) sets the password on the first visit (there is no
default and no password in the environment to leak). Passwords are salted
scrypt hashes -- stdlib only, no new dependency -- kept with the users, their
roles and any custom roles in `auth.json` next to the migration state file,
mode 0600, written atomically like the state file itself. Access control is
role-based: see core/rbac.py for the permission catalog and built-in roles
(admin, operator, read_only); further users and custom roles are managed by
anyone holding `users:manage` (Settings -> RBAC & Roles).

`auth.json` v1 held just `admin_password_hash`; it's migrated in place into a
single `admin` user on first load.

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
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.core.rbac import ADMIN_ROLE, ALL_PERMISSIONS, BUILTIN_ROLES, PERMISSIONS

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
    if password.lower() in {"password", "administrator", "couchbase", "changeme", "password1234", "operator1234", "readonly1234"}:
        return "That password is too common."
    return None


_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


def username_problem(username: str) -> str | None:
    if not _USERNAME_RE.match(username):
        return "Usernames are 2-32 characters: lowercase letters, digits, '.', '_' or '-'."
    return None


def role_name_problem(name: str) -> str | None:
    if not _ROLE_RE.match(name):
        return "Role names are 2-32 characters: lowercase letters, digits, '_' or '-'."
    return None


class AuthError(ValueError):
    """A user/role change that can't be made; `.status` is the HTTP code to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class User:
    username: str
    password_hash: str
    role: str
    created_at: str
    password_set_at: str
    disabled: bool = False

    def public(self) -> dict:
        return {"username": self.username, "role": self.role, "created_at": self.created_at,
                "password_set_at": self.password_set_at, "disabled": self.disabled}


@dataclass
class Role:
    name: str
    description: str
    permissions: list[str]
    builtin: bool = False

    def public(self, users_count: int = 0) -> dict:
        return {"name": self.name, "description": self.description, "permissions": sorted(self.permissions),
                "builtin": self.builtin, "users": users_count}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Session:
    username: str
    created: float
    last_seen: float
    source: str = "local"        # "local" (users above) or "ldap" (Settings -> LDAP)
    role: str | None = None      # fixed at login for ldap sessions

    def expired(self, now: float) -> bool:
        s = get_settings()
        return now - self.last_seen > s.session_idle_timeout_s or now - self.created > s.session_max_age_s


class AuthStore:
    _instance: AuthStore | None = None

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._custom_roles: dict[str, Role] = {}
        self._sessions: dict[str, Session] = {}
        self._load()

    @classmethod
    def instance(cls) -> AuthStore:
        if cls._instance is None:
            cls._instance = AuthStore()
        return cls._instance

    # ----------------------------------------------------------- persistence

    @staticmethod
    def path() -> Path:
        return Path(get_settings().migration_state_file).parent / "auth.json"

    def _load(self) -> None:
        p = self.path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not read %s: %s -- the admin password will have to be set again.", p, exc)
            return
        if "users" in raw:
            for name, u in raw.get("users", {}).items():
                self._users[name] = User(
                    username=name, password_hash=u["password_hash"], role=u.get("role", ADMIN_ROLE),
                    created_at=u.get("created_at") or _now(), password_set_at=u.get("password_set_at") or _now(),
                    disabled=bool(u.get("disabled", False)),
                )
            for name, r in raw.get("roles", {}).items():
                if name in BUILTIN_ROLES:
                    continue
                perms = [x for x in r.get("permissions", []) if x in PERMISSIONS]
                self._custom_roles[name] = Role(name=name, description=r.get("description", ""), permissions=perms)
        elif raw.get("admin_password_hash"):
            # v1 file: a lone administrator password. Migrate it into the `admin` user.
            when = raw.get("password_set_at") or _now()
            self._users[ADMIN_ROLE] = User(username=ADMIN_ROLE, password_hash=raw["admin_password_hash"],
                                           role=ADMIN_ROLE, created_at=when, password_set_at=when)
            logger.info("Migrated %s from the single-password format to users/roles.", p)
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not rewrite %s after migration: %s", p, exc)

    def _persist(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "users": {
                u.username: {"password_hash": u.password_hash, "role": u.role, "created_at": u.created_at,
                             "password_set_at": u.password_set_at, "disabled": u.disabled}
                for u in self._users.values()
            },
            "roles": {
                r.name: {"description": r.description, "permissions": sorted(r.permissions)}
                for r in self._custom_roles.values()
            },
        }
        fd, tmp = tempfile.mkstemp(prefix=".auth.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
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

    # ----------------------------------------------------------- state

    @property
    def login_enabled(self) -> bool:
        return get_settings().admin_login_enabled

    @property
    def password_set(self) -> bool:
        """The instance has been claimed: the `admin` user exists."""
        return ADMIN_ROLE in self._users

    @property
    def setup_required(self) -> bool:
        return self.login_enabled and not self.password_set

    @property
    def password_set_at(self) -> str | None:
        admin = self._users.get(ADMIN_ROLE)
        return admin.password_set_at if admin else None

    # ----------------------------------------------------------- passwords

    def set_password(self, password: str, username: str = ADMIN_ROLE) -> None:
        """Sets a user's password; for `admin` on a fresh instance this is first-run setup."""
        now = _now()
        user = self._users.get(username)
        if user is None:
            if username != ADMIN_ROLE:
                raise AuthError(404, f"No user named '{username}'.")
            self._users[username] = User(username=username, password_hash=hash_password(password), role=ADMIN_ROLE,
                                         created_at=now, password_set_at=now)
        else:
            user.password_hash = hash_password(password)
            user.password_set_at = now
        self._persist()

    def check_password(self, password: str, username: str = ADMIN_ROLE) -> bool:
        user = self._users.get(username)
        if user is None or user.disabled:
            return False
        return verify_password(password, user.password_hash)

    # ----------------------------------------------------------- roles

    def roles(self) -> dict[str, Role]:
        out = {name: Role(name=name, description=r["description"], permissions=list(r["permissions"]), builtin=True)
               for name, r in BUILTIN_ROLES.items()}
        out.update(self._custom_roles)
        return out

    def role(self, name: str) -> Role | None:
        return self.roles().get(name)

    def permissions_for(self, role_name: str) -> frozenset[str]:
        role = self.role(role_name)
        return frozenset(role.permissions) if role else frozenset()

    def list_roles(self) -> list[dict]:
        counts: dict[str, int] = {}
        for u in self._users.values():
            counts[u.role] = counts.get(u.role, 0) + 1
        return [r.public(counts.get(r.name, 0)) for r in self.roles().values()]

    @staticmethod
    def _clean_permissions(permissions: list[str]) -> list[str]:
        unknown = sorted(set(permissions) - ALL_PERMISSIONS)
        if unknown:
            raise AuthError(422, f"Unknown permission(s): {', '.join(unknown)}.")
        return sorted(set(permissions))

    def create_role(self, name: str, description: str, permissions: list[str]) -> dict:
        problem = role_name_problem(name)
        if problem:
            raise AuthError(422, problem)
        if name in self.roles():
            raise AuthError(409, f"A role named '{name}' already exists.")
        self._custom_roles[name] = Role(name=name, description=description.strip()[:200],
                                        permissions=self._clean_permissions(permissions))
        self._persist()
        return self._custom_roles[name].public()

    def update_role(self, name: str, description: str | None, permissions: list[str] | None) -> dict:
        if name in BUILTIN_ROLES:
            raise AuthError(409, f"'{name}' is a built-in role and can't be changed; create a custom role instead.")
        role = self._custom_roles.get(name)
        if role is None:
            raise AuthError(404, f"No role named '{name}'.")
        if description is not None:
            role.description = description.strip()[:200]
        if permissions is not None:
            role.permissions = self._clean_permissions(permissions)
        self._persist()
        # Members pick the new permission set up on their next request; nothing to revoke.
        return role.public(sum(1 for u in self._users.values() if u.role == name))

    def delete_role(self, name: str) -> None:
        if name in BUILTIN_ROLES:
            raise AuthError(409, f"'{name}' is a built-in role and can't be deleted.")
        if name not in self._custom_roles:
            raise AuthError(404, f"No role named '{name}'.")
        holders = sorted(u.username for u in self._users.values() if u.role == name)
        if holders:
            raise AuthError(409, f"Role '{name}' is still assigned to: {', '.join(holders)}. Reassign them first.")
        del self._custom_roles[name]
        self._persist()

    # ----------------------------------------------------------- users

    def user(self, username: str) -> User | None:
        return self._users.get(username)

    def list_users(self) -> list[dict]:
        return [u.public() for u in sorted(self._users.values(), key=lambda u: (u.username != ADMIN_ROLE, u.username))]

    def _admins_left_without(self, username: str) -> int:
        return sum(1 for u in self._users.values()
                   if u.username != username and not u.disabled and u.role == ADMIN_ROLE)

    def create_user(self, username: str, password: str, role: str) -> dict:
        problem = username_problem(username) or password_problem(password)
        if problem:
            raise AuthError(422, problem)
        if username in self._users:
            raise AuthError(409, f"A user named '{username}' already exists.")
        if role not in self.roles():
            raise AuthError(422, f"No role named '{role}'.")
        now = _now()
        self._users[username] = User(username=username, password_hash=hash_password(password), role=role,
                                     created_at=now, password_set_at=now)
        self._persist()
        return self._users[username].public()

    def update_user(self, username: str, *, role: str | None = None, disabled: bool | None = None,
                    password: str | None = None, acting_user: str | None = None) -> dict:
        user = self._users.get(username)
        if user is None:
            raise AuthError(404, f"No user named '{username}'.")
        if role is not None and role not in self.roles():
            raise AuthError(422, f"No role named '{role}'.")
        demoting = role is not None and role != ADMIN_ROLE and user.role == ADMIN_ROLE
        disabling = disabled is True and not user.disabled
        if (demoting or disabling) and user.role == ADMIN_ROLE and self._admins_left_without(username) == 0:
            raise AuthError(409, "That would leave no active administrator.")
        if disabling and acting_user == username:
            raise AuthError(409, "You can't disable your own account.")
        if password is not None:
            problem = password_problem(password)
            if problem:
                raise AuthError(422, problem)
            user.password_hash = hash_password(password)
            user.password_set_at = _now()
        if role is not None:
            user.role = role
        if disabled is not None:
            user.disabled = disabled
        self._persist()
        if password is not None or disabled is True or demoting:
            self.revoke_user_sessions(username)
        return user.public()

    def delete_user(self, username: str, acting_user: str | None = None) -> None:
        user = self._users.get(username)
        if user is None:
            raise AuthError(404, f"No user named '{username}'.")
        if username == acting_user:
            raise AuthError(409, "You can't delete your own account.")
        if user.role == ADMIN_ROLE and not user.disabled and self._admins_left_without(username) == 0:
            raise AuthError(409, "That would leave no active administrator.")
        del self._users[username]
        self._persist()
        self.revoke_user_sessions(username)

    # ----------------------------------------------------------- sessions

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, username: str = ADMIN_ROLE, *, source: str = "local",
                       role: str | None = None) -> tuple[str, int]:
        """Returns (token, expires_at_epoch_seconds)."""
        self._sweep()
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._sessions[self._key(token)] = Session(username=username, created=now, last_seen=now,
                                                   source=source, role=role)
        return token, int(now + get_settings().session_idle_timeout_s)

    def session_user(self, token: str | None) -> User | None:
        """The user behind a live session token (touching it), or None. For a
        directory (LDAP) session this is an ephemeral User carrying the role
        mapped at login; it has no password and isn't in `list_users()`."""
        if not token:
            return None
        key = self._key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        now = time.time()
        if session.expired(now):
            self._sessions.pop(key, None)
            return None
        if session.source == "ldap":
            if not session.role or session.role not in self.roles():
                self._sessions.pop(key, None)   # role deleted since login
                return None
            session.last_seen = now
            return User(username=session.username, password_hash="", role=session.role,
                        created_at="", password_set_at="", disabled=False)
        user = self._users.get(session.username)
        if user is None or user.disabled:
            self._sessions.pop(key, None)
            return None
        session.last_seen = now
        return user

    def session_source(self, token: str | None) -> str | None:
        if not token:
            return None
        session = self._sessions.get(self._key(token))
        return session.source if session else None

    def session_valid(self, token: str | None) -> bool:
        return self.session_user(token) is not None

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._key(token), None)

    def revoke_all_except(self, token: str | None, username: str | None = None) -> int:
        """Signs out every session (of `username`, when given) other than `token`'s."""
        keep = self._key(token) if token else None
        before = len(self._sessions)
        self._sessions = {k: v for k, v in self._sessions.items()
                          if k == keep or (username is not None and v.username != username)}
        return before - len(self._sessions)

    def revoke_user_sessions(self, username: str) -> int:
        before = len(self._sessions)
        self._sessions = {k: v for k, v in self._sessions.items() if v.username != username}
        return before - len(self._sessions)

    def _sweep(self) -> None:
        now = time.time()
        for key in [k for k, v in self._sessions.items() if v.expired(now)]:
            self._sessions.pop(key, None)

    def reset_for_tests(self) -> None:
        self._users.clear()
        self._custom_roles.clear()
        self._sessions.clear()
        p = self.path()
        if p.exists():
            p.unlink()
