"""
Persistence for migration records: a JSON file on a mounted volume, so the
default deployment needs no database beyond the Couchbase instance already used
for agent memory.

- Credentials inside each record are encrypted at rest (core/secrets.py).
- Writes are atomic (temp file + rename), so a crash mid-write can't truncate the
  file and lose every migration's checkpoint.

Run a single backend replica with this store (the Helm chart does). Swap it for a
Couchbase collection if you need several replicas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.core.secrets import SecretBox
from app.models.schemas import MigrationRecord

logger = logging.getLogger(__name__)


class MigrationStore:
    _instance: MigrationStore | None = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self._lock = asyncio.Lock()
        self._records: dict[str, MigrationRecord] = {}
        self._load()

    @classmethod
    def instance(cls) -> MigrationStore:
        if cls._instance is None:
            cls._instance = MigrationStore()
        return cls._instance

    def _path(self) -> Path:
        return Path(self.settings.migration_state_file)

    def _load(self) -> None:
        p = self._path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not parse migration state file %s: %s", p, exc)
            return
        box = SecretBox.instance()
        for mid, data in raw.items():
            try:
                self._records[mid] = MigrationRecord.model_validate(box.decrypt_tree(data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unreadable migration record %s: %s", mid, exc)

    def _persist(self) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        box = SecretBox.instance()
        payload = {
            mid: box.encrypt_tree(r.model_dump(mode="json")) for mid, r in self._records.items()
        }
        fd, tmp = tempfile.mkstemp(prefix=".migrations.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
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

    async def save(self, record: MigrationRecord) -> None:
        async with self._lock:
            self._records[str(record.migration_id)] = record
            await asyncio.to_thread(self._persist)

    async def get(self, migration_id: UUID) -> MigrationRecord | None:
        async with self._lock:
            return self._records.get(str(migration_id))

    async def list_all(self) -> list[MigrationRecord]:
        async with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

    async def delete(self, migration_id: UUID) -> None:
        async with self._lock:
            self._records.pop(str(migration_id), None)
            await asyncio.to_thread(self._persist)
