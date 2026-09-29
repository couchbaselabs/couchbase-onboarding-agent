"""
Post-cutover fallback: reverse sync from Couchbase back to the source.

After cutover, applications write to Couchbase. If something goes wrong days
later, falling back to the source is only safe if the source has those writes
too. Fallback sync keeps it current:

  1. At start, every destination key this migration covers is recorded (with the
     source-native identity from its _migration envelope) in a small SQLite file
     next to the state file, and a META().cas watermark is set just before the
     cutover time.
  2. Every FALLBACK_POLL_INTERVAL_S, documents whose CAS moved past the
     watermark are written back to the source through the connector's
     apply_reverse() (upserts), in CAS order.
  3. Every FALLBACK_DELETE_CHECK_INTERVAL_S, keys that disappeared from Couchbase
     are deleted at the source.

"Fall back now" runs one last drain and hands the system of record back to the
source (phase FELL_BACK). Stop application writes to Couchbase before using it.

Forward sync must be stopped (cutover done) before fallback starts, otherwise
the two directions would echo each other's writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from couchbase.exceptions import CouchbaseException
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import QueryOptions

from app.config import get_settings
from app.core.connectors.base import SourceConnector
from app.core.connectors.util import SeenRecently, strip_migration_envelope, utcnow
from app.core.couchbase_loader import CouchbaseLoader
from app.core.store import MigrationStore
from app.models.enums import MigrationPhase
from app.models.schemas import MigrationRecord

logger = logging.getLogger(__name__)

LogFn = Callable[[MigrationRecord, str], None]
EmitFn = Callable[[MigrationRecord], Awaitable[None]]


def _key_types(source_key: dict[str, Any] | None, types: dict[str, str] | None) -> dict[str, str] | None:
    """Only the type hints that describe key fields -- all a delete needs."""
    if not source_key or not types:
        return None
    wanted = {f"/{k}" for k in source_key}
    kept = {p: t for p, t in types.items() if p in wanted}
    return kept or None


class _KnownKeys:
    """Destination keys (per container) with the identity needed to delete them
    at the source, persisted so a restart doesn't lose track of deletes."""

    def __init__(self, migration_id: str):
        directory = Path(get_settings().migration_state_file).parent / "fallback"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{migration_id}.sqlite"
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS known (container TEXT, k TEXT, ident TEXT, PRIMARY KEY (container, k))"
        )
        self.db.commit()

    def upsert(self, container: str, rows: list[tuple[str, str]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO known(container, k, ident) VALUES (?, ?, ?)",
            [(container, k, ident) for k, ident in rows],
        )
        self.db.commit()

    def keys(self, container: str) -> list[tuple[str, str]]:
        return list(self.db.execute("SELECT k, ident FROM known WHERE container = ?", (container,)))

    def remove(self, container: str, keys: list[str]) -> None:
        self.db.executemany("DELETE FROM known WHERE container = ? AND k = ?", [(container, k) for k in keys])
        self.db.commit()

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM known LIMIT 1").fetchone() is None

    def close(self, *, delete_file: bool = False) -> None:
        self.db.close()
        if delete_file:
            try:
                os.unlink(self.path)
            except OSError:
                pass


class FallbackSync:
    def __init__(
        self,
        record: MigrationRecord,
        connector: SourceConnector,
        loader: CouchbaseLoader,
        containers: list[str],
        *,
        log: LogFn,
        emit: EmitFn,
    ):
        self.record = record
        self.connector = connector
        self.loader = loader
        self.containers = containers
        self.log = log
        self.emit = emit
        self.settings = get_settings()
        self.store = MigrationStore.instance()
        self.known = _KnownKeys(str(record.migration_id))
        self._seen = SeenRecently()

    # -- queries ----------------------------------------------------------------

    def _query(self, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        cluster = self.loader.client.connect()
        return list(cluster.query(
            statement,
            QueryOptions(named_parameters=params, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows())

    def _ks(self, container: str) -> str:
        scope, collection = self.loader.keyspace_for(container).split(".", 1)
        return f"`{self.loader.bucket_name}`.`{scope}`.`{collection}`"

    async def _snapshot_keys(self) -> None:
        for container in self.containers:
            rows = await asyncio.to_thread(
                self._query,
                f"SELECT META(d).id AS id, d._migration.source_key AS sk, d._migration.types AS t "
                f"FROM {self._ks(container)} AS d WHERE META(d).cas >= 0",
                {},
            )
            self.known.upsert(container, [
                (r["id"], json.dumps({"sk": r.get("sk"), "t": _key_types(r.get("sk"), r.get("t"))}))
                for r in rows
            ])

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Prepare state for a new fallback sync (not for a resume)."""
        fb = self.record.fallback
        cutover = self.record.cutover_at or datetime.utcnow()
        start_cas = int((cutover.timestamp() - self.settings.couchbase_cdc_overlap_s) * 1_000_000_000)
        fb.watermarks = {self.loader.keyspace_for(c): start_cas for c in self.containers}
        fb.active = True
        fb.started_at = datetime.utcnow()
        fb.stopped_at = None
        fb.changes_applied = fb.deletes_applied = fb.errors = 0
        fb.last_error = None
        warnings = await self.loader.ensure_indexes(self.containers, cas_index=True)
        for w in warnings:
            self.log(self.record, f"Fallback sync: {w}")
        await self._snapshot_keys()

    async def run(self) -> MigrationRecord:
        """Loop until the phase leaves FALLBACK_SYNCING. Resumable: state lives in
        record.fallback and the SQLite key file."""
        if self.known.is_empty():
            await self._snapshot_keys()
        last_delete_check = time.monotonic()
        backoff = 5.0
        try:
            while True:
                fresh = await self.store.get(self.record.migration_id)
                if fresh is None or fresh.phase != MigrationPhase.FALLBACK_SYNCING:
                    return fresh or self.record
                self.record = fresh
                try:
                    await self.drain_once()
                    if time.monotonic() - last_delete_check >= self.settings.fallback_delete_check_interval_s:
                        await self.propagate_deletes()
                        last_delete_check = time.monotonic()
                    backoff = 5.0
                except Exception as exc:  # noqa: BLE001
                    self.record.fallback.errors += 1
                    self.record.fallback.last_error = str(exc)
                    self.log(self.record, f"Fallback sync error (retrying in {backoff:.0f}s): {exc}")
                    await self.emit(self.record)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.settings.cdc_retry_max_backoff_s)
                    continue
                await self.emit(self.record)
                await asyncio.sleep(self.settings.fallback_poll_interval_s)
        finally:
            self.known.close()

    async def drain_once(self) -> int:
        """Apply every change past the watermarks. Returns how many were applied.

        CAS values are hybrid-logical-clock timestamps assigned per vBucket, so a
        write on one node can become visible with a CAS slightly below one
        already seen on another. Each poll therefore re-reads an overlap window
        below the watermark and skips (key, CAS) pairs it has already applied."""
        fb = self.record.fallback
        overlap = self.settings.couchbase_cdc_overlap_s * 1_000_000_000
        page = 500
        applied = 0
        newest_cas = 0
        for container in self.containers:
            keyspace = self.loader.keyspace_for(container)
            watermark = fb.watermarks.get(keyspace, 0)
            since = max(watermark - overlap, 0)
            max_seen = watermark
            while True:
                rows = await asyncio.to_thread(
                    self._query,
                    f"SELECT META(d).id AS id, META(d).cas AS cas, d AS doc FROM {self._ks(container)} AS d "
                    f"WHERE META(d).cas >= $since ORDER BY META(d).cas LIMIT {page}",
                    {"since": since},
                )
                fresh = 0
                known_rows: list[tuple[str, str]] = []
                for row in rows:
                    key, cas = row["id"], int(row["cas"])
                    max_seen = max(max_seen, cas)
                    if not self._seen.add((key, cas)):
                        continue
                    fresh += 1
                    body, envelope = strip_migration_envelope(row.get("doc") or {})
                    if self.loader.passthrough:
                        source_key: dict[str, Any] | None = {"key": key}
                    else:
                        source_key = envelope.get("source_key")
                    types = envelope.get("types")
                    await self.connector.apply_reverse(container, "upsert", key, body, source_key, types)
                    known_rows.append((key, json.dumps({
                        "sk": source_key or _derive_ident(body), "t": _key_types(source_key, types),
                    })))
                    newest_cas = max(newest_cas, cas)
                    applied += 1
                if known_rows:
                    await asyncio.to_thread(self.known.upsert, container, known_rows)
                if len(rows) < page or fresh == 0:
                    break
                since = max_seen
            fb.watermarks[keyspace] = max_seen
        fb.changes_applied += applied
        fb.last_poll_at = datetime.utcnow()
        if applied and newest_cas:
            fb.lag_seconds = max(0.0, time.time() - newest_cas / 1_000_000_000)
        elif not applied:
            fb.lag_seconds = 0.0
        if applied:
            self.log(self.record, f"Fallback sync: wrote {applied} change(s) from Couchbase back to the source.")
        return applied

    async def propagate_deletes(self) -> int:
        deleted = 0
        for container in self.containers:
            present = set()
            try:
                rows = await asyncio.to_thread(
                    self._query, f"SELECT RAW META(d).id FROM {self._ks(container)} AS d WHERE META(d).cas >= 0", {},
                )
                present = set(rows)
            except CouchbaseException as exc:
                raise RuntimeError(f"could not list destination keys for {container}: {exc}") from exc
            gone = [(k, ident) for k, ident in self.known.keys(container) if k not in present]
            if not gone:
                continue
            removed: list[str] = []
            for key, ident in gone:
                info = json.loads(ident or "{}")
                await self.connector.apply_reverse(container, "delete", key, None, info.get("sk"), info.get("t"))
                removed.append(key)
            await asyncio.to_thread(self.known.remove, container, removed)
            deleted += len(removed)
        if deleted:
            self.record.fallback.deletes_applied += deleted
            self.log(self.record, f"Fallback sync: deleted {deleted} document(s) at the source that were removed in Couchbase.")
        return deleted

    async def final_drain(self) -> None:
        await self.drain_once()
        await self.propagate_deletes()


def _derive_ident(body: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort identity for documents applications created after cutover
    (no _migration envelope): the connector's apply_reverse() falls back to the
    key and body, so only fields commonly used as identity are kept."""
    ident = {k: body[k] for k in ("_id", "id", "redis_key") if k in body}
    return ident or None


def discard_state(migration_id: str) -> None:
    path = Path(get_settings().migration_state_file).parent / "fallback" / f"{migration_id}.sqlite"
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = ["FallbackSync", "discard_state", "utcnow"]
