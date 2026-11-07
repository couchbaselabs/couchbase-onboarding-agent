"""
Post-cutover fallback: reverse sync from Couchbase back to the source.

After cutover, applications write to Couchbase. If something goes wrong days
later, falling back to the source is only safe if the source has those writes
too. Fallback sync keeps it current. Two methods, picked per source:

  CAS polling (every source):
  1. At start, every destination key this migration covers (its PRIMARY
     keyspace per container) is recorded, with the source-native identity from
     its _migration envelope, in a small SQLite file next to the state file, and
     a META().cas watermark is set just before the cutover time.
  2. Every FALLBACK_POLL_INTERVAL_S, documents whose CAS moved past the
     watermark are read in CAS order, coalesced per key and written back to
     the source in batches through the connector's apply_reverse_batch() --
     the source's native bulk API (Mongo bulk_write, DynamoDB BatchWriteItem,
     Redis pipelines, Cassandra concurrent execution, Couchbase *_multi).
     The watermark only moves past a page once it has been applied.
  3. Every FALLBACK_DELETE_CHECK_INTERVAL_S, keys that disappeared from
     Couchbase are deleted at the source (keys are paged, so a collection of
     any size fits in memory).

  Reverse XDCR (Couchbase Enterprise source + self-managed destination):
     a replication is created on the DESTINATION cluster pointing back at the
     source bucket. XDCR streams mutations and deletions from the destination's
     own change log (DCP) -- no polling, no index, and deletes propagate as
     tombstones. See ReverseXdcr below.

"Fall back now" captures the source's current change position (so forward
sync can later resume from exactly here -- "failback"), runs one last drain,
and hands the system of record back to the source (phase FELL_BACK). Stop
application writes to Couchbase before using it.

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
from app.core.connectors.base import ReverseOp, SourceConnector
from app.core.connectors.util import SeenRecently, strip_migration_envelope, utcnow
from app.core.couchbase_loader import CouchbaseLoader
from app.core.couchbase_native import CouchbaseNativeError, XdcrManager
from app.core.store import MigrationStore
from app.core.targets import parse_keyspace, qualify_keyspace
from app.models.enums import MigrationPhase
from app.models.schemas import FailedEvent, MigrationRecord

logger = logging.getLogger(__name__)

LogFn = Callable[[MigrationRecord, str], None]
EmitFn = Callable[[MigrationRecord], Awaitable[None]]

FALLBACK_METHOD_CAS_POLL = "cas_poll"
FALLBACK_METHOD_XDCR = "xdcr"
# Retries of keys the source rejected before they're recorded as failed events
# and the watermark moves on (same policy as the forward direction).
REVERSE_WRITE_RETRIES = 3
# Change rows read per N1QL page while draining.
DRAIN_PAGE = 500
# Keys per page when listing destination keys for delete detection.
KEY_PAGE = 5000


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

    def missing(self, container: str, present_table: str) -> list[tuple[str, str]]:
        return list(self.db.execute(
            f"SELECT k, ident FROM known WHERE container = ? AND k NOT IN (SELECT k FROM {present_table})",
            (container,),
        ))

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
    """CAS-polling reverse sync (see module docstring)."""

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

    def _keyspace(self, container: str) -> str:
        return self.loader.keyspace_for(container)

    def _ks(self, container: str) -> str:
        return parse_keyspace(self._keyspace(container), self.loader.bucket_name).n1ql

    async def _list_keys_paged(self, container: str):
        """Every key in the container's primary keyspace, KEY_PAGE at a time."""
        after = ""
        while True:
            rows = await asyncio.to_thread(
                self._query,
                f"SELECT META(d).id AS id, d._migration.source_key AS sk, d._migration.types AS t "
                f"FROM {self._ks(container)} AS d WHERE META(d).id > $after ORDER BY META(d).id LIMIT {KEY_PAGE}",
                {"after": after},
            )
            if not rows:
                return
            yield rows
            if len(rows) < KEY_PAGE:
                return
            after = rows[-1]["id"]

    async def _snapshot_keys(self) -> None:
        for container in self.containers:
            async for rows in self._list_keys_paged(container):
                await asyncio.to_thread(self.known.upsert, container, [
                    (r["id"], json.dumps({"sk": r.get("sk"), "t": _key_types(r.get("sk"), r.get("t"))}))
                    for r in rows
                ])

    def _watermark(self, keyspace: str) -> int:
        fb = self.record.fallback
        if keyspace in fb.watermarks:
            return fb.watermarks[keyspace]
        # Records from earlier versions keyed watermarks by scope.collection.
        for k, v in fb.watermarks.items():
            if qualify_keyspace(k, self.loader.bucket_name) == keyspace:
                return v
        return 0

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        """Prepare state for a new fallback sync (not for a resume)."""
        fb = self.record.fallback
        cutover = self.record.cutover_at or datetime.utcnow()
        start_cas = int((cutover.timestamp() - self.settings.couchbase_cdc_overlap_s) * 1_000_000_000)
        fb.method = FALLBACK_METHOD_CAS_POLL
        fb.watermarks = {self._keyspace(c): start_cas for c in self.containers}
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

    async def _apply(self, container: str, ops: dict[str, ReverseOp]) -> tuple[int, list[tuple[str, str]]]:
        """Write a coalesced batch back, retrying rejected keys. Returns
        (applied, still-failed)."""
        pending = list(ops.values())
        applied = 0
        failed: list[tuple[str, str]] = []
        for attempt in range(REVERSE_WRITE_RETRIES + 1):
            outcome = await self.connector.apply_reverse_batch(container, pending)
            applied += outcome.applied
            failed = outcome.failed
            if not failed or attempt == REVERSE_WRITE_RETRIES:
                break
            await asyncio.sleep(min(0.25 * (2 ** attempt), 5.0))
            retry = {k for k, _ in failed}
            pending = [op for op in pending if op.key in retry]
        return applied, failed

    def _record_failures(self, container: str, failed: list[tuple[str, str]], op: str) -> None:
        fb = self.record.fallback
        fb.errors += len(failed)
        for key, err in failed[:20]:
            self.record.failed_events.append(FailedEvent(container=container, key=key, op=f"reverse_{op}", error=err))
        self.record.failed_events = self.record.failed_events[-100:]
        if failed:
            fb.last_error = f"{len(failed)} write(s) to the source failed for {container} (e.g. {failed[0][1]})"

    async def drain_once(self) -> int:
        """Apply every change past the watermarks. Returns how many were applied.

        CAS values are hybrid-logical-clock timestamps assigned per vBucket, so a
        write on one node can become visible with a CAS slightly below one
        already seen on another. Each poll therefore re-reads an overlap window
        below the watermark and skips (key, CAS) pairs it has already applied."""
        fb = self.record.fallback
        overlap = self.settings.couchbase_cdc_overlap_s * 1_000_000_000
        applied = 0
        newest_cas = 0
        for container in self.containers:
            keyspace = self._keyspace(container)
            watermark = self._watermark(keyspace)
            since = max(watermark - overlap, 0)
            max_seen = watermark
            while True:
                rows = await asyncio.to_thread(
                    self._query,
                    f"SELECT META(d).id AS id, META(d).cas AS cas, d AS doc FROM {self._ks(container)} AS d "
                    f"WHERE META(d).cas >= $since ORDER BY META(d).cas LIMIT {DRAIN_PAGE}",
                    {"since": since},
                )
                ops: dict[str, ReverseOp] = {}
                known_rows: list[tuple[str, str]] = []
                versions: dict[str, int] = {}
                page_max = max_seen
                for row in rows:
                    key, cas = row["id"], int(row["cas"])
                    page_max = max(page_max, cas)
                    if self._seen.contains((key, cas)):
                        continue
                    versions[key] = cas
                    body, envelope = strip_migration_envelope(row.get("doc") or {})
                    if self.loader.passthrough:
                        source_key: dict[str, Any] | None = {"key": key}
                    else:
                        source_key = envelope.get("source_key")
                    types = envelope.get("types")
                    ops[key] = ReverseOp(op="upsert", key=key, body=body, source_key=source_key, types=types)
                    known_rows.append((key, json.dumps({
                        "sk": source_key or _derive_ident(body), "t": _key_types(source_key, types),
                    })))
                    newest_cas = max(newest_cas, cas)
                if ops:
                    done, failed = await self._apply(container, ops)
                    applied += done
                    failed_keys = {k for k, _ in failed}
                    for key, cas in versions.items():
                        if key not in failed_keys:
                            self._seen.add((key, cas))
                    if failed:
                        self._record_failures(container, failed, "upsert")
                    await asyncio.to_thread(self.known.upsert, container,
                                            [r for r in known_rows if r[0] not in failed_keys])
                max_seen = page_max
                if len(rows) < DRAIN_PAGE or not ops:
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
            self.known.db.execute("DROP TABLE IF EXISTS present")
            self.known.db.execute("CREATE TEMP TABLE present (k TEXT PRIMARY KEY)")
            try:
                async for rows in self._list_keys_paged(container):
                    await asyncio.to_thread(
                        self.known.db.executemany, "INSERT OR IGNORE INTO present(k) VALUES (?)",
                        [(r["id"],) for r in rows],
                    )
            except CouchbaseException as exc:
                raise RuntimeError(f"could not list destination keys for {container}: {exc}") from exc
            gone = await asyncio.to_thread(self.known.missing, container, "present")
            if not gone:
                continue
            ops: dict[str, ReverseOp] = {}
            for key, ident in gone:
                info = json.loads(ident or "{}")
                ops[key] = ReverseOp(op="delete", key=key, source_key=info.get("sk"), types=info.get("t"))
            done, failed = await self._apply(container, ops)
            if failed:
                self._record_failures(container, failed, "delete")
            removed = [k for k in ops if k not in {f for f, _ in failed}]
            await asyncio.to_thread(self.known.remove, container, removed)
            deleted += done
        if deleted:
            self.record.fallback.deletes_applied += deleted
            self.log(self.record, f"Fallback sync: deleted {deleted} document(s) at the source that were removed in Couchbase.")
        return deleted

    async def final_drain(self) -> None:
        await self.drain_once()
        await self.propagate_deletes()


class ReverseXdcr:
    """Reverse replication for a Couchbase Enterprise source with a self-managed
    destination: an XDCR replication created on the destination cluster, from
    the primary destination bucket back to the source bucket. Nothing runs in
    this process beyond progress polling; XDCR handles mutations and deletes
    from the destination's DCP stream.

    Not available when the destination is Capella (XDCR out of Capella goes
    through Capella's own management API); those migrations use FallbackSync."""

    def __init__(self, record: MigrationRecord, *, log: LogFn, emit: EmitFn):
        self.record = record
        self.log = log
        self.emit = emit
        self.settings = get_settings()
        self.store = MigrationStore.instance()
        self.xdcr = XdcrManager(record.plan.destination)

    @staticmethod
    def applicable(record: MigrationRecord) -> bool:
        from app.models.enums import SourceType

        return (
            record.plan.source.source_type == SourceType.COUCHBASE_ENTERPRISE
            and not record.plan.destination.is_capella
        )

    def _ref_name(self) -> str:
        return f"onboarding-agent-reverse-{self.record.migration_id}"

    def _buckets(self) -> tuple[str, str]:
        """(primary destination bucket, source bucket)."""
        from app.core.targets import buckets_in, target_map

        plan = self.record.plan
        targeted = buckets_in(target_map(plan.source, plan.destination_bucket, plan.containers))
        return (targeted[0] if targeted else plan.destination_bucket), plan.source.database or ""

    async def start(self) -> None:
        fb = self.record.fallback
        dest_bucket, source_bucket = self._buckets()
        fb.method = FALLBACK_METHOD_XDCR
        fb.active = True
        fb.started_at = datetime.utcnow()
        fb.stopped_at = None
        fb.changes_applied = fb.deletes_applied = fb.errors = 0
        fb.last_error = None
        ref = self._ref_name()
        await asyncio.to_thread(self.xdcr.create_remote_cluster_ref, self.record.plan.source, ref)
        repl_id: str | None = None
        for attempt in range(1, 6):
            try:
                repl_id = await asyncio.to_thread(self.xdcr.create_replication, dest_bucket, ref, source_bucket)
                break
            except CouchbaseNativeError as exc:
                if "remote cluster" not in str(exc).lower() or attempt == 5:
                    raise
                await asyncio.sleep(2.0)
        self.record.checkpoint["_reverse_xdcr_ref_name"] = ref
        self.record.checkpoint["_reverse_xdcr_replication_id"] = repl_id
        self.log(self.record, f"Reverse XDCR replication started ('{dest_bucket}' on the destination -> "
                              f"'{source_bucket}' on the source).")

    async def run(self) -> MigrationRecord:
        repl_id = self.record.checkpoint.get("_reverse_xdcr_replication_id")
        last_emit = 0.0
        while True:
            fresh = await self.store.get(self.record.migration_id)
            if fresh is None or fresh.phase != MigrationPhase.FALLBACK_SYNCING:
                return fresh or self.record
            self.record = fresh
            if repl_id:
                task = await asyncio.to_thread(self.xdcr.get_progress, repl_id)
                fb = self.record.fallback
                if not task:
                    fb.last_error = "The reverse XDCR replication no longer exists on the destination cluster."
                    fb.errors += 1
                else:
                    changes_left = task.get("changesLeft")
                    docs = task.get("docsWritten", task.get("docsChecked"))
                    if docs is not None:
                        fb.changes_applied = int(docs)
                    fb.lag_seconds = float(changes_left) if changes_left is not None else None
                    fb.last_poll_at = datetime.utcnow()
                    fb.last_error = None
            now = time.monotonic()
            if now - last_emit >= 1.0:
                last_emit = now
                await self.emit(self.record)
            await asyncio.sleep(self.settings.xdcr_poll_interval_s)

    async def final_drain(self) -> None:
        """Wait for changesLeft to reach 0 (bounded by the cutover drain
        timeout), then remove the replication."""
        repl_id = self.record.checkpoint.get("_reverse_xdcr_replication_id")
        deadline = time.monotonic() + self.settings.cutover_drain_timeout_s
        if repl_id:
            while time.monotonic() < deadline:
                task = await asyncio.to_thread(self.xdcr.get_progress, repl_id)
                if not task or int(task.get("changesLeft") or 0) == 0:
                    break
                await asyncio.sleep(self.settings.xdcr_poll_interval_s)
        await self.teardown()

    async def teardown(self) -> None:
        repl_id = self.record.checkpoint.pop("_reverse_xdcr_replication_id", None)
        ref = self.record.checkpoint.pop("_reverse_xdcr_ref_name", None)
        if repl_id:
            await asyncio.to_thread(self.xdcr.remove_replication, repl_id)
        if ref:
            await asyncio.to_thread(self.xdcr.remove_remote_cluster_ref, ref)
        if repl_id or ref:
            self.log(self.record, "Reverse XDCR replication removed from the destination cluster.")


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


__all__ = ["FallbackSync", "ReverseXdcr", "discard_state", "utcnow"]
