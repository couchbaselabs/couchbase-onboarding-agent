"""Settings -> Certificates: trust store and the UI serving certificate."""
from __future__ import annotations

import datetime as dt
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.api.security import throttle
from app.config import get_settings
from app.core.auth_store import AuthStore
from app.core.certs import ServingCert, TrustStore
from app.core.tls import requests_verify

STRONG = "correct-horse-battery-staple"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(cn: str, *, ca: bool, key=None, issuer=None, issuer_key=None, sans=(), days=365):
    key = key or _key()
    issuer_key = issuer_key or key
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer or name)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
    return b.sign(issuer_key, hashes.SHA256()), key


def _pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def admin(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_login_enabled", True)
    monkeypatch.setattr(settings, "api_auth_token", "")
    monkeypatch.setattr(settings, "migration_state_file", str(tmp_path / "state" / "migrations.json"))
    monkeypatch.setattr(settings, "tls_publish_dir", str(tmp_path / "publish"))
    monkeypatch.setattr(settings, "tls_publish_secret", "")
    (tmp_path / "publish").mkdir()
    AuthStore._instance = None
    TrustStore._instance = None
    ServingCert._instance = None
    throttle.reset()
    from app.main import app

    client = TestClient(app)
    token = client.post("/api/auth/setup", json={"password": STRONG}).json()["token"]
    yield client, {"Authorization": f"Bearer {token}"}, tmp_path
    TrustStore.instance().reset_for_tests()
    ServingCert.instance().reset_for_tests()
    AuthStore.instance().reset_for_tests()
    AuthStore._instance = TrustStore._instance = ServingCert._instance = None
    throttle.reset()


def test_trust_store_bundle_and_env(admin):
    client, h, _ = admin
    assert client.get("/api/settings/certificates/trusted", headers=h).json() == {"certificates": [], "bundle_path": None}
    assert "SSL_CERT_FILE" not in os.environ or not os.environ["SSL_CERT_FILE"].endswith("ca-bundle.pem")

    class NoCa:
        ca_cert_path = None
        ca_cert_pem = None
        tls_skip_verify = False
    assert requests_verify(NoCa()) is True  # nothing uploaded -> library defaults

    root, _ = _cert("Corp Root CA", ca=True)
    leaf, _ = _cert("server.corp", ca=False)
    r = client.post("/api/settings/certificates/trusted", json={"pem": _pem(leaf)}, headers=h)
    assert r.status_code == 422 and "not a CA" in r.json()["detail"]
    r = client.post("/api/settings/certificates/trusted", json={"pem": _pem(root) + _pem(root)}, headers=h)
    assert r.status_code == 201 and len(r.json()["added"]) == 1
    listed = client.get("/api/settings/certificates/trusted", headers=h).json()
    assert listed["certificates"][0]["subject"] == "Corp Root CA" and listed["certificates"][0]["is_ca"]
    bundle = listed["bundle_path"]
    assert bundle and os.environ["SSL_CERT_FILE"] == bundle == os.environ["REQUESTS_CA_BUNDLE"]
    text = open(bundle).read()
    assert "Corp Root CA" in text and text.count("BEGIN CERTIFICATE") > 1  # system CAs + ours
    assert requests_verify(NoCa()) == bundle

    class OwnCa(NoCa):
        ca_cert_pem = _pem(root)
    assert requests_verify(OwnCa()) != bundle  # per-connection CA still wins

    fp = listed["certificates"][0]["fingerprint"]
    assert client.delete(f"/api/settings/certificates/trusted/{fp[:20]}", headers=h).status_code == 204
    assert client.delete(f"/api/settings/certificates/trusted/{fp}", headers=h).status_code == 404
    assert client.get("/api/settings/certificates/trusted", headers=h).json()["bundle_path"] is None
    assert "SSL_CERT_FILE" not in os.environ


def test_serving_cert_install_and_publish(admin):
    client, h, tmp = admin
    assert client.get("/api/settings/certificates/server", headers=h).json()["installed"] is False

    ca, ca_key = _cert("Corp Issuing CA", ca=True)
    key = _key()
    leaf, _ = _cert("onboarding.corp", ca=False, key=key, issuer=ca.subject, issuer_key=ca_key, sans=("onboarding.corp",))
    other = _key()

    r = client.put("/api/settings/certificates/server", json={"certificate_pem": _pem(leaf), "private_key_pem": _key_pem(other)}, headers=h)
    assert r.status_code == 422 and "doesn't match" in r.json()["detail"]
    r = client.put("/api/settings/certificates/server", json={"certificate_pem": _pem(ca), "private_key_pem": _key_pem(ca_key)}, headers=h)
    assert r.status_code == 422 and "CA certificate" in r.json()["detail"]

    # chain pasted root-first is reordered leaf-first
    r = client.put("/api/settings/certificates/server",
                   json={"certificate_pem": _pem(ca) + _pem(leaf), "private_key_pem": _key_pem(key)}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installed"] and body["certificate"]["subject"] == "onboarding.corp"
    assert body["chain"][0]["subject"] == "Corp Issuing CA" and body["published"].startswith("directory")
    published = (tmp / "publish" / "server.crt").read_text()
    assert published.startswith("-----BEGIN CERTIFICATE-----") and published.count("BEGIN CERTIFICATE") == 2
    assert (tmp / "publish" / "server.key").exists()
    assert oct((tmp / "state" / "certs" / "server" / "server.key").stat().st_mode)[-3:] == "600"

    r = client.delete("/api/settings/certificates/server", headers=h)
    assert r.status_code == 200 and r.json()["installed"] is False
    assert not (tmp / "publish" / "server.crt").exists()


def test_settings_need_settings_write(admin):
    client, h, _ = admin
    client.post("/api/auth/users", json={"username": "ops", "password": STRONG, "role": "operator"}, headers=h)
    tok = client.post("/api/auth/login", json={"username": "ops", "password": STRONG}).json()["token"]
    r = client.get("/api/settings/certificates/trusted", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403 and "settings:write" in r.json()["detail"]
