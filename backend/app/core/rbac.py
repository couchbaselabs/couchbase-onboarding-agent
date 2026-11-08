"""Role-based access control: the permission catalog, the built-in roles and
the rule that maps an API request to the permission it needs.

Permissions are coarse on purpose -- few enough to reason about on a settings
page, fine enough to separate "look" from "touch" from "destroy":

  migrations:read     view migrations, progress, stats, logs, live updates
  migrations:write    create, validate, approve, start, stop, resume, reconcile
  migrations:rollback rollback, fallback and failback (reversing a cutover)
  migrations:delete   delete migration records
  sources:test        test source/destination connections and inspect topology
  agent:chat          ask the assistant (chat, replication-mode recommendation)
  settings:write      change the assistant's model / provider, LDAP, certificates, log forwarding
  users:manage        manage users and roles (RBAC)

Built-in roles can't be edited or deleted; custom roles are any subset of the
catalog. The static API_AUTH_TOKEN always acts as `admin`.
"""
from __future__ import annotations

from dataclasses import dataclass

PERMISSIONS: dict[str, str] = {
    "migrations:read": "View migrations, progress, stats, logs and live updates",
    "migrations:write": "Create, validate, approve, start, stop, resume and reconcile migrations",
    "migrations:rollback": "Roll back a migration, start/stop fallback and fail back",
    "migrations:delete": "Delete migration records",
    "sources:test": "Test source and destination connections and inspect their topology",
    "agent:chat": "Ask the assistant (chat and replication-mode recommendations)",
    "settings:write": "Change settings: assistant model, LDAP, certificates, log forwarding",
    "users:manage": "Manage users and roles",
}

ALL_PERMISSIONS: frozenset[str] = frozenset(PERMISSIONS)

ADMIN_ROLE = "admin"
OPERATOR_ROLE = "operator"
READ_ONLY_ROLE = "read_only"

BUILTIN_ROLES: dict[str, dict] = {
    ADMIN_ROLE: {
        "description": "Full access, including users, roles and the assistant's settings.",
        "permissions": sorted(ALL_PERMISSIONS),
    },
    OPERATOR_ROLE: {
        "description": "Runs migrations end to end -- create, validate, start, cut over, roll back -- "
                       "but can't change settings or manage users.",
        "permissions": ["migrations:read", "migrations:write", "migrations:rollback", "migrations:delete",
                        "sources:test", "agent:chat"],
    },
    READ_ONLY_ROLE: {
        "description": "Watches migrations and asks the assistant; can't change anything.",
        "permissions": ["migrations:read", "agent:chat"],
    },
}


@dataclass(frozen=True)
class Principal:
    """Who a request is acting as, once the bearer has been accepted."""
    username: str
    role: str
    permissions: frozenset[str]
    method: str  # "token" (static API_AUTH_TOKEN), "session" (login) or "open" (auth off)
    source: str = "local"  # for sessions: "local" user or "ldap" directory account

    def can(self, permission: str | None) -> bool:
        return permission is None or permission in self.permissions


ANONYMOUS_OPEN = Principal(username="anonymous", role=ADMIN_ROLE, permissions=ALL_PERMISSIONS, method="open")
API_TOKEN_PRINCIPAL = Principal(username="api-token", role=ADMIN_ROLE, permissions=ALL_PERMISSIONS, method="token")


def required_permission(method: str, path: str) -> str | None:
    """The permission a request needs, or None when being authenticated is
    enough (health/config are not routed through here at all).

    Kept in one place rather than on each route so a new endpoint is denied
    by default for non-admins unless it's classified here: anything under
    /api that isn't matched falls through to `users:manage`.
    """
    method = method.upper()
    p = path.rstrip("/")
    if p.startswith("/api"):
        p = p[4:]

    if p.startswith("/auth/users") or p.startswith("/auth/roles") or p == "/auth/permissions":
        return "users:manage"
    if p.startswith("/auth"):
        return None  # session, logout, own password change: any authenticated caller
    if p.startswith("/ws/"):
        return "migrations:read"
    if p.startswith("/migrations"):
        if method == "GET":
            return "migrations:read"
        if method == "DELETE":
            return "migrations:delete"
        tail = p.split("/", 3)[3] if p.count("/") >= 3 else ""
        if tail.startswith(("rollback", "fallback", "failback")):
            return "migrations:rollback"
        return "migrations:write"
    if p.startswith("/stats"):
        return "migrations:read"
    if p.startswith("/settings"):
        return "settings:write"
    if p.startswith("/sources"):
        return "sources:test"
    if p.startswith("/agent"):
        if p == "/agent/status":
            return None
        if p.startswith("/agent/provider"):
            return "settings:write"
        return "agent:chat"
    return "users:manage"
