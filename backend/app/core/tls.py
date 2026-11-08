"""TLS verification settings shared by every REST call and CLI invocation that
talks to a source or destination cluster.

Verification is on by default. A connection can trust a specific CA (pasted PEM
or a path inside the container) or, for throwaway test clusters only, opt out
with tls_skip_verify -- which the validator reports as a warning. With neither,
the CAs uploaded under Settings -> Certificates (core/certs.py) are trusted in
addition to the system bundle.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class _TlsConfig(Protocol):
    ca_cert_path: str | None
    ca_cert_pem: str | None
    tls_skip_verify: bool


def ca_file_for(config: _TlsConfig) -> str | None:
    """Path of a CA bundle to trust for this connection, materializing a pasted
    PEM into the scratch directory (content-addressed, mode 0600) if needed."""
    pem = getattr(config, "ca_cert_pem", None)
    if pem and pem.strip():
        digest = hashlib.sha256(pem.encode("utf-8")).hexdigest()[:16]
        directory = Path(get_settings().scratch_dir) / "ca"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.pem"
        if not path.exists():
            path.write_text(pem.strip() + "\n")
            os.chmod(path, 0o600)
        return str(path)
    return getattr(config, "ca_cert_path", None) or default_ca_bundle()


def default_ca_bundle() -> str | None:
    """The merged system + uploaded-CA bundle, or None when nothing was uploaded."""
    from app.core.certs import TrustStore  # noqa: PLC0415

    return TrustStore.instance().bundle_path()


def sdk_connection_string(connection_string: str, config: _TlsConfig) -> str:
    """Couchbase SDK connection string with tls_verify=none appended when the
    connection opted out of verification."""
    if not getattr(config, "tls_skip_verify", False) or "tls_verify=" in connection_string:
        return connection_string
    sep = "&" if "?" in connection_string else "?"
    return f"{connection_string}{sep}tls_verify=none"


def sdk_authenticator(username: str, password: str, config: _TlsConfig):
    """PasswordAuthenticator that trusts the connection's CA, if one was given."""
    from couchbase.auth import PasswordAuthenticator

    ca_file = ca_file_for(config)
    if ca_file:
        return PasswordAuthenticator(username, password, cert_path=ca_file)
    return PasswordAuthenticator(username, password)


def requests_verify(config: _TlsConfig) -> bool | str:
    """Value for the `verify=` argument of requests calls."""
    if getattr(config, "tls_skip_verify", False):
        return False
    return ca_file_for(config) or True
