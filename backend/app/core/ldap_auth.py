"""LDAP / Active Directory sign-in (Settings -> LDAP).

Flow for a login that isn't a local user:
  1. connect to `server_url` (ldaps:// or ldap:// + StartTLS; the server
     certificate is verified against the system CAs plus Settings ->
     Certificates), bind with the service account (or anonymously);
  2. find the user: `user_filter` with {username} substituted, under
     `user_base_dn`;
  3. collect the user's groups -- the `group_attribute` on the entry
     (memberOf, the AD / OpenLDAP-with-memberof default) or, if that's empty,
     a `group_filter` search with {user_dn} / {username} substituted;
  4. bind as the user's DN with the typed password (the actual credential check);
  5. map groups to a role: the first `role_mappings` entry whose group DN the
     user holds wins; otherwise `default_role`; otherwise the login is denied.

Nothing about directory users is stored locally except the live session
(username, role, source=ldap). The bind password is kept Fernet-encrypted in
`<state>/ldap.json` with the STATE_ENCRYPTION_KEY, like migration credentials.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import get_settings
from app.core.secrets import SecretBox

logger = logging.getLogger(__name__)
audit = logging.getLogger("onboarding_agent.auth")

AD_USER_FILTER = "(&(objectClass=user)(sAMAccountName={username}))"
OPENLDAP_USER_FILTER = "(&(objectClass=inetOrgPerson)(uid={username}))"
DEFAULT_GROUP_FILTER = "(&(objectClass=groupOfNames)(member={user_dn}))"

PRESETS = {
    "active_directory": {"label": "Active Directory", "user_filter": AD_USER_FILTER, "group_attribute": "memberOf",
                         "group_filter": "", "username_attribute": "sAMAccountName"},
    "openldap": {"label": "OpenLDAP / 389ds", "user_filter": OPENLDAP_USER_FILTER, "group_attribute": "memberOf",
                 "group_filter": DEFAULT_GROUP_FILTER, "username_attribute": "uid"},
}


class LdapError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RoleMapping:
    group: str   # group DN (case-insensitive match)
    role: str


@dataclass
class LdapConfig:
    enabled: bool = False
    server_url: str = ""              # ldap://host:389 or ldaps://host:636
    start_tls: bool = False           # upgrade an ldap:// connection with StartTLS
    verify_tls: bool = True
    bind_dn: str = ""                 # service account; empty = anonymous bind for the search
    bind_password: str = ""           # stored encrypted; "" on read = unchanged
    user_base_dn: str = ""
    user_filter: str = AD_USER_FILTER
    username_attribute: str = "sAMAccountName"
    group_attribute: str = "memberOf"
    group_base_dn: str = ""
    group_filter: str = ""
    role_mappings: list[RoleMapping] = field(default_factory=list)
    default_role: str = ""            # "" = users in no mapped group are denied
    timeout_s: int = 10

    def public(self) -> dict:
        d = asdict(self)
        d["bind_password"] = ""
        d["bind_password_set"] = bool(self.bind_password)
        return d


@dataclass
class LdapIdentity:
    username: str
    dn: str
    groups: list[str]
    role: str | None
    display_name: str = ""


ConnectionFactory = Callable[["LdapConfig", str | None, str | None], object]


class LdapStore:
    _instance: LdapStore | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._config = LdapConfig()
        self._load()

    @classmethod
    def instance(cls) -> LdapStore:
        if cls._instance is None:
            cls._instance = LdapStore()
        return cls._instance

    @staticmethod
    def path() -> Path:
        return Path(get_settings().migration_state_file).parent / "ldap.json"

    def _load(self) -> None:
        p = self.path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            raw["role_mappings"] = [RoleMapping(**m) for m in raw.get("role_mappings", [])]
            if raw.get("bind_password"):
                raw["bind_password"] = SecretBox.instance().decrypt(raw["bind_password"])
            self._config = LdapConfig(**{k: v for k, v in raw.items() if k in LdapConfig.__dataclass_fields__})
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not read %s: %s -- LDAP sign-in disabled until it's saved again.", p, exc)

    def _persist(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self._config)
        if data["bind_password"]:
            data["bind_password"] = SecretBox.instance().encrypt(data["bind_password"])
        fd, tmp = tempfile.mkstemp(prefix=".ldap.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
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
    def config(self) -> LdapConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.server_url)

    def save(self, incoming: dict, known_roles: set[str]) -> LdapConfig:
        cfg = LdapConfig(**{k: v for k, v in incoming.items() if k in LdapConfig.__dataclass_fields__ and k != "role_mappings"})
        cfg.role_mappings = [RoleMapping(group=m["group"].strip(), role=m["role"].strip())
                             for m in incoming.get("role_mappings", []) if m.get("group") and m.get("role")]
        if not cfg.bind_password:
            cfg.bind_password = self._config.bind_password  # "" from the UI means "keep"
        self.validate(cfg, known_roles)
        with self._lock:
            self._config = cfg
            self._persist()
        audit.warning("ldap: configuration saved (enabled=%s, server=%s, %d role mapping(s))",
                      cfg.enabled, cfg.server_url, len(cfg.role_mappings))
        return cfg

    @staticmethod
    def validate(cfg: LdapConfig, known_roles: set[str]) -> None:
        if cfg.enabled or cfg.server_url:
            if not cfg.server_url.startswith(("ldap://", "ldaps://")):
                raise LdapError(422, "Server URL must start with ldap:// or ldaps://.")
            if not cfg.user_base_dn:
                raise LdapError(422, "User base DN is required.")
            if "{username}" not in cfg.user_filter:
                raise LdapError(422, "The user filter must contain {username}.")
            if cfg.bind_dn and not cfg.bind_password:
                raise LdapError(422, "A bind password is required with a bind DN.")
            if not cfg.group_attribute and not cfg.group_filter:
                raise LdapError(422, "Give a group attribute (memberOf) or a group search filter.")
            if cfg.group_filter and "{user_dn}" not in cfg.group_filter and "{username}" not in cfg.group_filter:
                raise LdapError(422, "The group filter must contain {user_dn} or {username}.")
        for m in cfg.role_mappings:
            if m.role not in known_roles:
                raise LdapError(422, f"Role mapping for '{m.group}' uses unknown role '{m.role}'.")
        if cfg.default_role and cfg.default_role not in known_roles:
            raise LdapError(422, f"Unknown default role '{cfg.default_role}'.")
        if cfg.enabled and not cfg.role_mappings and not cfg.default_role:
            raise LdapError(422, "Add at least one group -> role mapping or a default role, or nobody can sign in via LDAP.")

    def reset_for_tests(self) -> None:
        self._config = LdapConfig()
        p = self.path()
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------- authenticator

def _default_connection_factory(cfg: LdapConfig, user: str | None, password: str | None):
    """A bound ldap3 Connection (service account when user is None)."""
    import ldap3  # noqa: PLC0415
    from ldap3.core.exceptions import LDAPException  # noqa: PLC0415

    from app.core.certs import TrustStore  # noqa: PLC0415

    tls = None
    if cfg.server_url.startswith("ldaps://") or cfg.start_tls:
        bundle = TrustStore.instance().bundle_path()
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if cfg.verify_tls else ssl.CERT_NONE,
                        ca_certs_file=bundle, version=ssl.PROTOCOL_TLS_CLIENT)
    server = ldap3.Server(cfg.server_url, tls=tls, connect_timeout=cfg.timeout_s, get_info=ldap3.NONE)
    bind_user = user if user is not None else (cfg.bind_dn or None)
    bind_pw = password if user is not None else (cfg.bind_password or None)
    try:
        conn = ldap3.Connection(server, user=bind_user, password=bind_pw, auto_bind=False,
                                receive_timeout=cfg.timeout_s, raise_exceptions=False)
        if cfg.start_tls and not cfg.server_url.startswith("ldaps://"):
            conn.open()
            if not conn.start_tls():
                raise LdapError(502, f"StartTLS failed: {conn.result}")
        if not conn.bind():
            raise LdapError(401 if user is not None else 502,
                            "Wrong username or password." if user is not None
                            else f"Service-account bind failed: {conn.result.get('description', conn.result)}")
        return conn
    except LDAPException as exc:
        raise LdapError(502, f"LDAP connection failed: {exc}") from exc


class LdapAuthenticator:
    def __init__(self, store: LdapStore | None = None, connection_factory: ConnectionFactory | None = None) -> None:
        self.store = store or LdapStore.instance()
        self.connect = connection_factory or _default_connection_factory

    @staticmethod
    def _escape(value: str) -> str:
        from ldap3.utils.conv import escape_filter_chars  # noqa: PLC0415

        return escape_filter_chars(value)

    def lookup(self, cfg: LdapConfig, username: str) -> LdapIdentity | None:
        """Find the user and their groups with the service account. None = no such user."""
        import ldap3  # noqa: PLC0415

        conn = self.connect(cfg, None, None)
        try:
            attrs = [a for a in {cfg.group_attribute, cfg.username_attribute, "cn", "displayName"} if a]
            flt = cfg.user_filter.replace("{username}", self._escape(username))
            if not conn.search(cfg.user_base_dn, flt, search_scope=ldap3.SUBTREE, attributes=attrs, size_limit=2):
                return None
            entries = [e for e in conn.entries]
            if not entries:
                return None
            if len(entries) > 1:
                raise LdapError(409, f"The user filter matched {len(entries)} entries for '{username}'; make it unique.")
            entry = entries[0]
            dn = entry.entry_dn
            groups: list[str] = []
            if cfg.group_attribute and cfg.group_attribute in entry.entry_attributes:
                groups = [str(g) for g in entry[cfg.group_attribute].values]
            if not groups and cfg.group_filter:
                gflt = cfg.group_filter.replace("{user_dn}", self._escape(dn)).replace("{username}", self._escape(username))
                if conn.search(cfg.group_base_dn or cfg.user_base_dn, gflt, search_scope=ldap3.SUBTREE, attributes=["cn"]):
                    groups = [e.entry_dn for e in conn.entries]
            display = ""
            for a in ("displayName", "cn"):
                if a in entry.entry_attributes and entry[a].value:
                    display = str(entry[a].value)
                    break
            return LdapIdentity(username=username, dn=dn, groups=groups, role=self.map_role(cfg, groups),
                                display_name=display)
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def map_role(cfg: LdapConfig, groups: list[str]) -> str | None:
        held = {g.lower() for g in groups}
        for m in cfg.role_mappings:
            if m.group.lower() in held:
                return m.role
        return cfg.default_role or None

    def authenticate(self, username: str, password: str, cfg: LdapConfig | None = None) -> LdapIdentity:
        """Full login: lookup, bind as the user, map the role. Raises LdapError."""
        cfg = cfg or self.store.config
        if not password:
            raise LdapError(401, "Wrong username or password.")
        ident = self.lookup(cfg, username)
        if ident is None:
            raise LdapError(401, "Wrong username or password.")
        conn = self.connect(cfg, ident.dn, password)
        try:
            pass
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
        if ident.role is None:
            audit.warning("ldap: %s authenticated but holds no mapped group (%s) -- denied", username, ", ".join(ident.groups) or "no groups")
            raise LdapError(403, "Your directory account isn't in a group that's allowed to use this application.")
        return ident

    def test(self, cfg: LdapConfig, username: str | None = None, password: str | None = None) -> dict:
        """Settings -> LDAP -> Test: service bind, then optionally a trial login."""
        conn = self.connect(cfg, None, None)
        try:
            server_info = {"bound_as": cfg.bind_dn or "anonymous"}
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001
                pass
        out: dict = {"ok": True, "connection": server_info}
        if username:
            ident = self.lookup(cfg, username)
            if ident is None:
                out.update(ok=False, user=None, message=f"No entry matched the user filter for '{username}'.")
                return out
            out["user"] = {"dn": ident.dn, "groups": ident.groups, "role": ident.role, "display_name": ident.display_name}
            if password:
                try:
                    self.connect(cfg, ident.dn, password).unbind()
                    out["login"] = "ok"
                except LdapError as exc:
                    out.update(ok=False, login="failed", message=str(exc))
            if ident.role is None:
                out.update(ok=False, message="User found but none of their groups map to a role (and there is no default role).")
        return out
