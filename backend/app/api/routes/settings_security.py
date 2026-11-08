"""Security settings (Settings -> Certificates / LDAP / Log forwarding).

Mounted at /api/settings behind the bearer dependency; core/rbac.py maps the
whole prefix to `settings:write`, which only `admin` holds by default.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app.core.auth_store import AuthStore
from app.core.certs import CertError, ServingCert, TrustStore
from app.core.ldap_auth import PRESETS, LdapAuthenticator, LdapConfig, LdapError, LdapStore, RoleMapping
from app.core.log_forwarding import (
    DESTINATIONS,
    LEVELS,
    Forwarder,
    ForwardError,
    ForwardStore,
    send_test_event,
)

router = APIRouter()


def _cert(fn):
    try:
        return fn()
    except CertError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


# ---------------------------------------------------------------- trusted CAs

class PemUpload(BaseModel):
    pem: str = Field(min_length=1, max_length=512_000)


@router.get("/certificates/trusted")
async def list_trusted() -> dict:
    store = TrustStore.instance()
    return {"certificates": store.list(), "bundle_path": store.bundle_path()}


@router.post("/certificates/trusted", status_code=201)
async def add_trusted(body: PemUpload) -> dict:
    added = _cert(lambda: TrustStore.instance().add(body.pem))
    return {"added": added, "certificates": TrustStore.instance().list()}


@router.delete("/certificates/trusted/{fingerprint}", status_code=204, response_class=Response)
async def remove_trusted(fingerprint: str) -> Response:
    _cert(lambda: TrustStore.instance().remove(fingerprint))
    return Response(status_code=204)


# ---------------------------------------------------------------- serving certificate

class ServingCertUpload(BaseModel):
    certificate_pem: str = Field(min_length=1, max_length=512_000, description="Leaf first, then the chain")
    private_key_pem: str = Field(min_length=1, max_length=64_000, description="Unencrypted PEM private key")


@router.get("/certificates/server")
async def serving_cert_status() -> dict:
    return ServingCert.instance().status()


@router.put("/certificates/server")
async def install_serving_cert(body: ServingCertUpload) -> dict:
    return _cert(lambda: ServingCert.instance().install(body.certificate_pem, body.private_key_pem))


@router.delete("/certificates/server")
async def remove_serving_cert() -> dict:
    return ServingCert.instance().remove()


# ---------------------------------------------------------------- LDAP

class RoleMappingIn(BaseModel):
    group: str = Field(min_length=1, max_length=512)
    role: str = Field(min_length=1, max_length=64)


class LdapConfigIn(BaseModel):
    enabled: bool = False
    server_url: str = Field(default="", max_length=512)
    start_tls: bool = False
    verify_tls: bool = True
    bind_dn: str = Field(default="", max_length=512)
    bind_password: str = Field(default="", max_length=512)   # "" = keep the stored one
    user_base_dn: str = Field(default="", max_length=512)
    user_filter: str = Field(default="", max_length=1024)
    username_attribute: str = Field(default="sAMAccountName", max_length=64)
    group_attribute: str = Field(default="memberOf", max_length=64)
    group_base_dn: str = Field(default="", max_length=512)
    group_filter: str = Field(default="", max_length=1024)
    role_mappings: list[RoleMappingIn] = Field(default_factory=list)
    default_role: str = Field(default="", max_length=64)
    timeout_s: int = Field(default=10, ge=1, le=120)


class LdapTestIn(LdapConfigIn):
    test_username: str = Field(default="", max_length=256)
    test_password: str = Field(default="", max_length=512)


def _ldap(fn):
    try:
        return fn()
    except LdapError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _cfg_from(body: LdapConfigIn) -> LdapConfig:
    """A config for testing: unsaved values, falling back to the stored bind password."""
    data = body.model_dump(exclude={"test_username", "test_password"})
    data["role_mappings"] = [RoleMapping(**m) for m in data["role_mappings"]]
    cfg = LdapConfig(**data)
    if not cfg.bind_password:
        cfg.bind_password = LdapStore.instance().config.bind_password
    return cfg


@router.get("/ldap")
async def get_ldap() -> dict:
    return {"config": LdapStore.instance().config.public(), "presets": PRESETS,
            "roles": sorted(AuthStore.instance().roles())}


@router.put("/ldap")
async def save_ldap(body: LdapConfigIn) -> dict:
    cfg = _ldap(lambda: LdapStore.instance().save(body.model_dump(), set(AuthStore.instance().roles())))
    return {"config": cfg.public()}


@router.post("/ldap/test")
async def test_ldap(body: LdapTestIn) -> dict:
    cfg = _cfg_from(body)
    _ldap(lambda: LdapStore.validate(cfg, set(AuthStore.instance().roles())))
    return _ldap(lambda: LdapAuthenticator().test(cfg, body.test_username or None, body.test_password or None))


# ---------------------------------------------------------------- log forwarding

class ForwardConfigIn(BaseModel):
    enabled: bool = False
    destination: str = Field(default="splunk_hec", max_length=32)
    settings: dict[str, str] = Field(default_factory=dict)
    min_level: str = Field(default="INFO", max_length=16)
    include_audit: bool = True
    include_app: bool = True
    batch_size: int = Field(default=50, ge=1, le=1000)
    flush_interval_s: int = Field(default=5, ge=1, le=300)
    hostname: str = Field(default="", max_length=253)
    extra_fields: dict[str, str] = Field(default_factory=dict)


def _fwd(fn):
    try:
        return fn()
    except ForwardError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/log-forwarding")
async def get_log_forwarding() -> dict:
    return {"config": ForwardStore.instance().config.public(), "destinations": DESTINATIONS, "levels": LEVELS,
            "status": Forwarder.instance().status()}


@router.put("/log-forwarding")
async def save_log_forwarding(body: ForwardConfigIn) -> dict:
    cfg = _fwd(lambda: ForwardStore.instance().save(body.model_dump()))
    return {"config": cfg.public(), "status": Forwarder.instance().status()}


@router.post("/log-forwarding/test")
async def test_log_forwarding(body: ForwardConfigIn) -> dict:
    store = ForwardStore.instance()
    cfg = _fwd(lambda: store.merge({**body.model_dump(), "enabled": True}))
    return _fwd(lambda: send_test_event(cfg))
