"""Encrypts credentials inside persisted migration records (core/store.py).

Only the fields named in models.schemas.SECRET_FIELDS are encrypted; the rest of
the state file stays readable for debugging. Values are stored as
"enc:v1:<fernet token>" so plaintext files from older versions load unchanged
and get encrypted on their next save.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.models.schemas import SECRET_FIELDS

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


class SecretBox:
    _instance: "SecretBox | None" = None

    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @classmethod
    def instance(cls) -> "SecretBox":
        if cls._instance is None:
            cls._instance = SecretBox(_load_or_create_key())
        return cls._instance

    def encrypt(self, value: str) -> str:
        if value.startswith(_PREFIX):
            return value
        return _PREFIX + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value.startswith(_PREFIX):
            return value
        try:
            return self._fernet.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.error(
                "Could not decrypt a stored credential -- STATE_ENCRYPTION_KEY changed or the key "
                "file was lost. The affected migration will need its credentials re-entered."
            )
            return ""

    def encrypt_tree(self, data: Any) -> Any:
        return _walk(data, self.encrypt)

    def decrypt_tree(self, data: Any) -> Any:
        return _walk(data, self.decrypt)


def _walk(data: Any, fn) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in SECRET_FIELDS and isinstance(v, str) and v:
                out[k] = fn(v)
            else:
                out[k] = _walk(v, fn)
        return out
    if isinstance(data, list):
        return [_walk(v, fn) for v in data]
    return data


def _load_or_create_key() -> bytes:
    settings = get_settings()
    if settings.state_encryption_key:
        return settings.state_encryption_key.encode("ascii")
    key_path = Path(settings.migration_state_file).parent / ".state.key"
    if key_path.exists():
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    logger.warning(
        "Generated a state encryption key at %s. Set STATE_ENCRYPTION_KEY to manage it yourself "
        "(required if the state volume is backed up or shared).", key_path,
    )
    return key
