"""Certificates managed from Settings -> Certificates.

Two separate things live here:

* **Trust store** -- CA certificates this app should trust when it connects
  out: an LDAPS directory, Couchbase / Capella clusters, a hosted LLM endpoint
  behind a private CA. Uploaded PEMs are kept one file per certificate under
  `<state>/certs/trusted/` and merged with the system bundle (certifi) into
  `<state>/certs/ca-bundle.pem`. That bundle becomes the default `verify=`
  for requests (core/tls.py), the Couchbase SDK's `cert_path`, httpx (via the
  SSL_CERT_FILE environment variable, which httpx reads when it builds a
  client) and the LDAP client. A per-connection CA pasted in the migration
  wizard still wins for that connection.

* **Serving certificate** -- the certificate + private key the HTTPS UI
  presents. The backend validates the pair, stores it under
  `<state>/certs/server/` and *publishes* it to wherever the frontend's nginx
  reads `/etc/nginx/tls` from:
    - a shared directory (`TLS_PUBLISH_DIR`; docker-compose mounts the
      frontend's tls volume into the backend at /data/tls), or
    - a Kubernetes TLS Secret (`TLS_PUBLISH_SECRET`; the Helm chart grants the
      backend's ServiceAccount update rights on exactly that Secret).
  The frontend container watches its cert files and reloads nginx.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from app.config import get_settings

logger = logging.getLogger(__name__)
audit = logging.getLogger("onboarding_agent.auth")


class CertError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class CertInfo:
    fingerprint: str        # sha256, hex, lowercase
    subject: str
    issuer: str
    not_before: str
    not_after: str
    is_ca: bool
    self_signed: bool
    sans: list[str]
    expired: bool

    @classmethod
    def of(cls, cert: x509.Certificate) -> CertInfo:
        def name(n: x509.Name) -> str:
            cn = n.get_attributes_for_oid(NameOID.COMMON_NAME)
            return cn[0].value if cn else n.rfc4514_string()

        is_ca = False
        try:
            is_ca = bool(cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca)
        except x509.ExtensionNotFound:
            pass
        sans: list[str] = []
        try:
            san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
            sans = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                   [str(v) for v in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            pass
        now = datetime.now(timezone.utc)
        return cls(
            fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
            subject=name(cert.subject), issuer=name(cert.issuer),
            not_before=cert.not_valid_before_utc.isoformat(timespec="seconds"),
            not_after=cert.not_valid_after_utc.isoformat(timespec="seconds"),
            is_ca=is_ca, self_signed=cert.subject == cert.issuer, sans=sans,
            expired=cert.not_valid_after_utc < now,
        )


def parse_pem_certificates(pem: str) -> list[x509.Certificate]:
    data = pem.strip().encode("utf-8")
    if b"-----BEGIN CERTIFICATE-----" not in data:
        raise CertError(422, "That doesn't look like a PEM certificate (no BEGIN CERTIFICATE block).")
    try:
        certs = x509.load_pem_x509_certificates(data)
    except ValueError as exc:
        raise CertError(422, f"Couldn't parse the PEM: {exc}") from exc
    if not certs:
        raise CertError(422, "No certificate found in the PEM.")
    return certs


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _write_private(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cert.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ====================================================================== trust store

class TrustStore:
    _instance: TrustStore | None = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> TrustStore:
        if cls._instance is None:
            cls._instance = TrustStore()
            cls._instance.apply()
        return cls._instance

    @staticmethod
    def root() -> Path:
        return Path(get_settings().migration_state_file).parent / "certs"

    def _dir(self) -> Path:
        return self.root() / "trusted"

    def bundle_file(self) -> Path:
        return self.root() / "ca-bundle.pem"

    def _certs(self) -> list[tuple[Path, x509.Certificate]]:
        out = []
        d = self._dir()
        if not d.exists():
            return out
        for p in sorted(d.glob("*.pem")):
            try:
                out.append((p, x509.load_pem_x509_certificate(p.read_bytes())))
            except ValueError:
                logger.warning("Ignoring unreadable trusted certificate %s", p)
        return out

    def list(self) -> list[dict]:
        return [asdict(CertInfo.of(c)) for _, c in self._certs()]

    def add(self, pem: str) -> list[dict]:
        """Adds every certificate in `pem`; returns the ones that were new."""
        certs = parse_pem_certificates(pem)
        added = []
        with self._lock:
            have = {CertInfo.of(c).fingerprint for _, c in self._certs()}
            for cert in certs:
                info = CertInfo.of(cert)
                if not info.is_ca:
                    raise CertError(422, f"'{info.subject}' is not a CA certificate (BasicConstraints CA is false); "
                                         "upload the issuing CA, not a server certificate.")
                if info.fingerprint in have:
                    continue
                have.add(info.fingerprint)
                _write_private(self._dir() / f"{info.fingerprint[:32]}.pem", _pem(cert), 0o644)
                added.append(asdict(info))
            self._rebuild()
        for a in added:
            audit.warning("certs: trusted CA added: %s (sha256 %s..., expires %s)", a["subject"], a["fingerprint"][:16], a["not_after"])
        return added

    def remove(self, fingerprint: str) -> None:
        fingerprint = fingerprint.lower()
        with self._lock:
            for p, c in self._certs():
                info = CertInfo.of(c)
                if info.fingerprint == fingerprint or info.fingerprint.startswith(fingerprint):
                    p.unlink()
                    self._rebuild()
                    audit.warning("certs: trusted CA removed: %s", info.subject)
                    return
        raise CertError(404, "No trusted certificate with that fingerprint.")

    def _rebuild(self) -> None:
        """System bundle + uploads -> ca-bundle.pem; point every TLS client at it."""
        own = self._certs()
        if not own:
            try:
                self.bundle_file().unlink()
            except FileNotFoundError:
                pass
            self.apply()
            return
        parts = [Path(certifi.where()).read_text()]
        parts += [f"# {CertInfo.of(c).subject}\n{_pem(c)}" for _, c in own]
        _write_private(self.bundle_file(), "\n".join(parts), 0o644)
        self.apply()

    def bundle_path(self) -> str | None:
        """Path of the merged bundle, or None when nothing was uploaded (use defaults)."""
        p = self.bundle_file()
        return str(p) if p.exists() else None

    def apply(self) -> None:
        """Export the bundle to the environment variables httpx / requests /
        libcurl consult, so clients created from now on trust the uploads."""
        path = self.bundle_path()
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            if path:
                os.environ[var] = path
            else:
                os.environ.pop(var, None)

    def reset_for_tests(self) -> None:
        shutil.rmtree(self._dir(), ignore_errors=True)
        try:
            self.bundle_file().unlink()
        except FileNotFoundError:
            pass
        self.apply()


# ====================================================================== serving cert

def _load_private_key(pem: str):
    try:
        return serialization.load_pem_private_key(pem.strip().encode("utf-8"), password=None)
    except TypeError as exc:
        raise CertError(422, "The private key is encrypted; upload it without a passphrase.") from exc
    except ValueError as exc:
        raise CertError(422, f"Couldn't parse the private key: {exc}") from exc


def _key_matches(cert: x509.Certificate, key) -> bool:
    pub = cert.public_key()
    if isinstance(key, rsa.RSAPrivateKey) and isinstance(pub, rsa.RSAPublicKey):
        return key.public_key().public_numbers() == pub.public_numbers()
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(pub, ec.EllipticCurvePublicKey):
        return key.public_key().public_numbers() == pub.public_numbers()
    if isinstance(key, ed25519.Ed25519PrivateKey) and isinstance(pub, ed25519.Ed25519PublicKey):
        return key.public_key().public_bytes_raw() == pub.public_bytes_raw()
    return False


class ServingCert:
    """The UI's HTTPS certificate: validate, store, publish."""

    _instance: ServingCert | None = None

    @classmethod
    def instance(cls) -> ServingCert:
        if cls._instance is None:
            cls._instance = ServingCert()
        return cls._instance

    @staticmethod
    def dir() -> Path:
        return TrustStore.root() / "server"

    def _files(self) -> tuple[Path, Path]:
        return self.dir() / "server.crt", self.dir() / "server.key"

    def status(self) -> dict:
        crt, _ = self._files()
        publish = self._publish_target()
        if not crt.exists():
            return {"installed": False, "certificate": None, "chain": [], "publish": publish}
        certs = x509.load_pem_x509_certificates(crt.read_bytes())
        return {
            "installed": True,
            "certificate": asdict(CertInfo.of(certs[0])),
            "chain": [asdict(CertInfo.of(c)) for c in certs[1:]],
            "installed_at": datetime.fromtimestamp(crt.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "publish": publish,
        }

    def _publish_target(self) -> dict:
        s = get_settings()
        if s.tls_publish_secret:
            return {"mode": "kubernetes-secret", "target": f"{self._k8s_namespace()}/{s.tls_publish_secret}"}
        if s.tls_publish_dir:
            return {"mode": "directory", "target": s.tls_publish_dir}
        return {"mode": "none", "target": None,
                "note": "No publish target configured (TLS_PUBLISH_DIR or TLS_PUBLISH_SECRET); the certificate is "
                        "stored but the UI keeps serving its current one."}

    def install(self, certificate_pem: str, private_key_pem: str) -> dict:
        certs = parse_pem_certificates(certificate_pem)
        key = _load_private_key(private_key_pem)
        # The leaf is whichever certificate the key belongs to (people paste chains root-first too).
        matches = [i for i, c in enumerate(certs) if _key_matches(c, key)]
        if not matches:
            raise CertError(422, "The private key doesn't match any certificate in the PEM.")
        i = matches[0]
        certs = [certs[i]] + certs[:i] + certs[i + 1:]
        leaf = certs[0]
        info = CertInfo.of(leaf)
        if info.is_ca and not info.sans:
            # A CA with no SANs can't name a server; a self-signed server cert
            # made with `openssl req -x509` is CA:TRUE but carries SANs, so it passes.
            raise CertError(422, f"'{info.subject}' is a CA certificate; upload the server certificate (leaf first, then its chain).")
        if info.expired:
            raise CertError(422, f"That certificate expired on {info.not_after}.")
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode("ascii")
        crt_pem = "".join(_pem(c) for c in certs)
        crt, keyf = self._files()
        _write_private(crt, crt_pem, 0o644)
        _write_private(keyf, key_pem, 0o600)
        published = self.publish(crt_pem, key_pem)
        audit.warning("certs: UI serving certificate installed: %s (expires %s, SANs %s) -> %s",
                      info.subject, info.not_after, ", ".join(info.sans) or "-", published)
        return {**self.status(), "published": published}

    def remove(self) -> dict:
        crt, keyf = self._files()
        for p in (crt, keyf):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        s = get_settings()
        note = "The UI keeps its current certificate until the frontend restarts (it then generates a self-signed one)."
        if s.tls_publish_dir:
            for name in ("server.crt", "server.key"):
                try:
                    (Path(s.tls_publish_dir) / name).unlink()
                except FileNotFoundError:
                    pass
        audit.warning("certs: UI serving certificate removed")
        return {**self.status(), "note": note}

    # ------------------------------------------------------------ publishing

    def publish(self, crt_pem: str, key_pem: str) -> str:
        s = get_settings()
        if s.tls_publish_secret:
            self._publish_k8s(s.tls_publish_secret, crt_pem, key_pem)
            return f"kubernetes secret {self._k8s_namespace()}/{s.tls_publish_secret}"
        if s.tls_publish_dir:
            d = Path(s.tls_publish_dir)
            try:
                # Readable by the nginx user in the other container (different uid);
                # the directory itself is private to the two containers.
                _write_private(d / "server.crt", crt_pem, 0o644)
                _write_private(d / "server.key", key_pem, 0o644)
            except OSError as exc:
                raise CertError(500, f"Stored, but couldn't publish to {d}: {exc}") from exc
            return f"directory {d}"
        return "stored only (no publish target configured)"

    @staticmethod
    def _k8s_namespace() -> str:
        ns = get_settings().tls_publish_namespace
        if ns:
            return ns
        try:
            return Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
        except OSError:
            return "default"

    def _publish_k8s(self, secret_name: str, crt_pem: str, key_pem: str) -> None:
        sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        try:
            token = (sa / "token").read_text().strip()
        except OSError as exc:
            raise CertError(500, "Stored, but this pod has no ServiceAccount token to update the TLS Secret with "
                                 "(enable frontend.tls.managedByApp in the chart).") from exc
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        url = f"https://{host}:{port}/api/v1/namespaces/{self._k8s_namespace()}/secrets/{secret_name}"
        body = {
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
            "metadata": {"name": secret_name},
            "data": {"tls.crt": base64.b64encode(crt_pem.encode()).decode(),
                     "tls.key": base64.b64encode(key_pem.encode()).decode()},
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/strategic-merge-patch+json"}
        resp = requests.patch(url, data=json.dumps(body), headers=headers, verify=str(sa / "ca.crt"), timeout=15)
        if resp.status_code == 404:
            headers["Content-Type"] = "application/json"
            resp = requests.post(url.rsplit("/", 1)[0], data=json.dumps(body), headers=headers,
                                 verify=str(sa / "ca.crt"), timeout=15)
        if resp.status_code >= 300:
            raise CertError(500, f"Stored, but updating Secret {secret_name} failed: HTTP {resp.status_code} "
                                 f"{resp.text[:200]}")

    def reset_for_tests(self) -> None:
        shutil.rmtree(self.dir(), ignore_errors=True)


def bundle_fingerprint() -> str | None:
    """Short content hash of the merged bundle (for the UI / tests)."""
    p = TrustStore.instance().bundle_path()
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16] if p else None
