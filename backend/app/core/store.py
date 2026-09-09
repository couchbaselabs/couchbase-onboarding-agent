"""
Lightweight persistence for migration records. Deliberately simple (JSON file on a
mounted volume) so the reference implementation has no hard dependency on an
additional database beyond the Couchbase EE instance already used for agent memory.
Swap this for a Couchbase collection or Postgres table in production if you need
multiple backend replicas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from uuid import UUID

from app.config import get_settings
from app.models.schemas import MigrationRecord

logger = logging.getLogger(__name__)


class MigrationStore:
    _instance: "MigrationStore | None" = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self._lock = asyncio.Lock()
        self._records: dict[str, MigrationRecord] = {}
        self._load()

    @classmethod
    def instance(cls) -> "MigrationStore":
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
            for mid, data in raw.items():
                self._records[mid] = MigrationRecord.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load migration state file %s: %s", p, exc)

    def _persist(self) -> None:
        p = self._path()
        os.makedirs(p.parent, exist_ok=True)
        payload = {mid: json.loads(r.model_dump_json()) for mid, r in self._records.items()}
        p.write_text(json.dumps(payload, indent=2, default=str))

    async def save(self, record: MigrationRecord) -> None:
        async with self._lock:
            self._records[str(record.migration_id)] = record
            self._persist()

    async def get(self, migration_id: UUID) -> MigrationRecord | None:
        async with self._lock:
            return self._records.get(str(migration_id))

    async def list_all(self) -> list[MigrationRecord]:
        async with self._lock:
            return sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)

    async def delete(self, migration_id: UUID) -> None:
        async with self._lock:
            self._records.pop(str(migration_id), None)
            self._persist()
