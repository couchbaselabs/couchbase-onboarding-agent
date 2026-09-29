"""
Orchestrates a migration end to end.

  One-time (FULL_LOAD):
    validate -> approve -> bulk copy -> verify -> COMPLETE

  Continuous (CDC_LIVE):
    validate -> approve -> continuous sync -> REPLICATING until cutover or stop

  Bulk copy + continuous sync (FULL_LOAD_AND_CDC):
    validate -> approve -> record the source's change position -> bulk copy ->
    catch up on changes made during the copy -> REPLICATING until cutover or stop

Continuous sync works the same way for every source (see _sync_loop):
  * The change-capture position is recorded BEFORE the bulk copy starts, so
    writes made while it runs are replayed afterwards instead of lost. Writes are
    idempotent upserts/deletes keyed deterministically, so replaying a change the
    copy already saw is harmless.
  * Change events are applied in micro-batches, coalesced per key (the last
    change wins), with per-event retries. Isolated failures are recorded on the
    migration instead of blocking the stream; a batch the destination rejects
    entirely restarts the stream from the last checkpoint.
  * The checkpoint is persisted after the batch is written, so a crash replays
    at most about a second of changes.
  * A dropped stream is retried with exponential backoff from the last
    checkpoint; too many consecutive failures fail the migration (it can be
    resumed later from the same checkpoint).
  * Periodic delete reconciliation (core/reconcile.py) removes documents deleted
    at the source for sources whose change capture can't see deletes.
  * Cutover drains the stream until it reports it is caught up, verifies
    per-container counts, and marks the migration COMPLETE.

Couchbase sources:
  * Enterprise Edition: cbbackupmgr for the bulk copy and XDCR for continuous
    replication (the native path at the bottom of this module).
  * Community Edition and Capella: cbbackupmgr for the bulk copy, then CAS
    polling through the same sync loop as every other source (documents written
    unmodified -- "passthrough").

Rollback (plan_rollback / rollback) undoes the destination side precisely:
collections the migration created are dropped, its documents in pre-existing
collections are removed by tag, and documents it overwrote or deleted there are
restored from the pre-image journal (see couchbase_loader.py). After cutover,
fallback sync (core/fallback.py) writes Couchbase changes back to the source so
applications can move back without losing writes.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable

from app.config import get_settings
from app.core import runner
from app.core.bottleneck_detector import (
    MAX_AUTO_THROTTLE_ATTEMPTS,
    MIN_AUTO_THROTTLE_CONCURRENCY,
    BottleneckMonitor,
)
from app.core.capella_client import CapellaClient
from app.core.connectors.base import ChangeEvent, SourceConnector, SourceConnectorError, SourceDocument
from app.core.connectors.couchbase_source import CouchbaseSourceConnector
from app.core.connectors.registry import get_connector
from app.core.connectors.util import utcnow
from app.core.couchbase_client import CouchbaseClusterClient
from app.core.couchbase_loader import CouchbaseLoader
from app.core.couchbase_native import CbBackupMgrRunner, CouchbaseNativeError, XdcrManager
from app.core.fallback import FallbackSync, discard_state
from app.core.reconcile import reconcile
from app.core.store import MigrationStore
from app.core.targets import target_map
from app.core.validator import MigrationValidator
from app.memory.couchbase_memory import AgentMemoryStore
from app.models.enums import (
    CONTINUOUS_STRATEGIES,
    COUCHBASE_SOURCE_TYPES,
    CdcMethod,
    MigrationPhase,
    MigrationStrategy,
    SourceType,
    SyncStage,
)
from app.models.schemas import (
    BottleneckFinding,
    ContainerVerification,
    FailedEvent,
    MigrationRecord,
    RollbackPlan,
    RollbackRequest,
)

logger = logging.getLogger(__name__)
settings = get_settings()

ProgressCallback = Callable[[MigrationRecord], Awaitable[None]]

# How often stats/bottleneck findings are recomputed and emitted while the full
# load or replication loop is running, independent of how often individual
# batches/events complete.
EMIT_INTERVAL_S = 1.0

# Matches the item count cbbackupmgr prints in its own progress lines, e.g.
# "Transferring key value data for... at 1.51MiB/s (about 2m34s remaining)
# 51010 items / 103.46MiB" or "Copied all data in 1m4s (Avg. 8.88MiB/Sec)
# 280241 items / 568.49MiB". Used to drive the dashboard's live docs-migrated/
# throughput stats during a native (cbbackupmgr) full load -- see
# _run_native_full_load() below.
CBBACKUPMGR_PROGRESS_RE = re.compile(r"([\d,]+)\s+items\s*/\s*[\d.]+\s*(?:B|KiB|MiB|GiB)")

# Phases rollback can start from. ROLLING_BACK/ROLLED_BACK are excluded (already
# done or underway) and FALLBACK_SYNCING must be stopped first.
_ROLLBACK_PHASES = {
    MigrationPhase.APPROVED, MigrationPhase.MIGRATING, MigrationPhase.REPLICATING,
    MigrationPhase.VERIFYING, MigrationPhase.COMPLETE, MigrationPhase.FAILED,
    MigrationPhase.STOPPED, MigrationPhase.CANCELLED, MigrationPhase.FELL_BACK,
}


def is_couchbase_source(record: MigrationRecord) -> bool:
    return record.plan.source.source_type in COUCHBASE_SOURCE_TYPES


def uses_xdcr(record: MigrationRecord) -> bool:
    return record.plan.source.source_type == SourceType.COUCHBASE_ENTERPRISE


def _has_bulk_copy(record: MigrationRecord) -> bool:
    return record.plan.strategy in (MigrationStrategy.FULL_LOAD, MigrationStrategy.FULL_LOAD_AND_CDC)


class _Rate:
    """Events per second over a sliding window."""

    def __init__(self, window_s: float = 30.0):
        self.window_s = window_s
        self._samples: deque[tuple[float, int]] = deque()

    def add(self, n: int) -> None:
        self._samples.append((time.monotonic(), n))

    def per_sec(self) -> float:
        now = time.monotonic()
        while self._samples and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        if not self._samples:
            return 0.0
        span = max(now - self._samples[0][0], 1.0)
        return sum(n for _, n in self._samples) / span


async def _micro_batches(
    stream: AsyncIterator[ChangeEvent], size: int, max_wait_s: float,
) -> AsyncIterator[list[ChangeEvent]]:
    """Group a change stream into batches of at most `size` events, flushed after
    `max_wait_s` or at the next heartbeat. Connectors heartbeat every few
    seconds while idle, so a partial batch never waits long."""
    batch: list[ChangeEvent] = []
    started: float | None = None
    async for event in stream:
        batch.append(event)
        if event.op == "heartbeat":
            yield batch
            batch, started = [], None
            continue
        if started is None:
            started = time.monotonic()
        if len(batch) >= size or time.monotonic() - started >= max_wait_s:
            yield batch
            batch, started = [], None
    if batch:
        yield batch


def _event_key(event: ChangeEvent) -> str | None:
    if event.op == "upsert" and event.document is not None:
        return event.document.key
    return event.key


class MigrationEngine:
    def __init__(self, on_progress: ProgressCallback | None = None):
        self.store = MigrationStore.instance()
        self.on_progress = on_progress
        self._last_emit: dict[str, float] = {}

    async def _emit(self, record: MigrationRecord, *, throttle_s: float = 0.0) -> None:
        key = str(record.migration_id)
        now = time.monotonic()
        if throttle_s and now - self._last_emit.get(key, 0.0) < throttle_s:
            return
        self._last_emit[key] = now
        record.updated_at = utcnow()
        await self.store.save(record)
        if self.on_progress:
            await self.on_progress(record)

    def _log(self, record: MigrationRecord, line: str) -> None:
        record.log_tail.append(f"[{utcnow().isoformat(timespec='seconds')}Z] {line}")
        record.log_tail = record.log_tail[-200:]
        logger.info("migration %s: %s", record.migration_id, line)

    def _included_containers(self, record: MigrationRecord) -> list[str]:
        return [c.container_name for c in record.plan.containers if c.include]

    def _target_map(self, record: MigrationRecord) -> dict[str, tuple[str, str]]:
        return target_map(record.plan.source, record.plan.containers)

    def _loader(self, record: MigrationRecord, client: CouchbaseClusterClient, *, journal: bool = True) -> CouchbaseLoader:
        rs = record.rollback_state
        journal_keyspaces: set[str] = set()
        if journal and record.plan.journal_overwrites:
            if rs.captured:
                journal_keyspaces = set(rs.preexisting_collections)
            else:
                # Rollback state wasn't captured (record from an older version):
                # journal every target, which is always safe.
                journal_keyspaces = {f"{s}.{c}" for s, c in self._target_map(record).values()}
        return CouchbaseLoader(
            client, record.plan.destination_bucket, record.migration_id, self._target_map(record),
            concurrency=record.plan.concurrency, passthrough=is_couchbase_source(record),
            journal_keyspaces=journal_keyspaces,
        )

    def _record_finding(self, record: MigrationRecord, raw, *, phase: str) -> BottleneckFinding:
        finding = BottleneckFinding(
            kind=raw.kind, phase=phase, message=raw.message, suggestion=raw.suggestion,
            recommended_concurrency=raw.recommended_concurrency,
        )
        record.bottleneck_findings.append(finding)
        record.bottleneck_findings = record.bottleneck_findings[-20:]
        self._log(record, f"Bottleneck detected ({raw.kind.value}, {phase}): {raw.message} Suggestion: {raw.suggestion}")
        return finding

    async def _remember(self, kind: str, payload: dict, migration_id) -> None:
        try:
            await AgentMemoryStore.instance().remember(kind, payload, migration_id=str(migration_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write agent memory (%s): %s", kind, exc)

    async def _fresh(self, record: MigrationRecord) -> MigrationRecord | None:
        return await self.store.get(record.migration_id)

    @staticmethod
    def _taken_over(record: MigrationRecord) -> bool:
        """True once a rollback (or a pause/cutover from another request) has
        claimed the migration, so a running bulk copy / sync must not move the
        phase forward again."""
        return record.phase in (
            MigrationPhase.ROLLING_BACK, MigrationPhase.ROLLED_BACK, MigrationPhase.STOPPED,
            MigrationPhase.COMPLETE, MigrationPhase.CANCELLED,
        )

    # -- validation / approval -----------------------------------------------

    async def validate(self, record: MigrationRecord) -> MigrationRecord:
        record.phase = MigrationPhase.VALIDATING
        self._log(record, "Running pre-migration validation checks...")
        await self._emit(record)

        validator = MigrationValidator(
            record.migration_id, record.plan.source, record.plan.destination,
            record.plan.strategy, record.plan.containers,
            destination_bucket=record.plan.destination_bucket,
            reconcile_interval_minutes=record.plan.reconcile_interval_minutes,
            journal_overwrites=record.plan.journal_overwrites,
        )
        report = await validator.run()
        record.validation_report = report
        record.phase = MigrationPhase.AWAITING_APPROVAL if report.passed else MigrationPhase.VALIDATION_FAILED
        self._log(
            record,
            f"Validation {'passed' if report.passed else 'FAILED'} "
            f"({sum(c.passed for c in report.checks)}/{len(report.checks)} checks OK).",
        )
        await self._emit(record)
        return record

    async def approve(self, record: MigrationRecord, approved_by: str) -> MigrationRecord:
        if record.phase != MigrationPhase.AWAITING_APPROVAL:
            raise ValueError(f"Migration is not awaiting approval (current phase: {record.phase.value}).")
        record.phase = MigrationPhase.APPROVED
        record.approved_by = approved_by
        record.approved_at = utcnow()
        self._log(record, f"Migration approved by {approved_by}.")
        await self._emit(record)
        return record

    def _ensure_destination_bucket(self, record: MigrationRecord) -> bool:
        try:
            if record.plan.destination.is_capella:
                created = CapellaClient().ensure_bucket_exists(
                    record.plan.destination, record.plan.destination_bucket,
                    ram_quota_mb=record.plan.destination_bucket_ram_quota_mb,
                )
            else:
                client = CouchbaseClusterClient(record.plan.destination)
                try:
                    created = client.ensure_bucket(
                        record.plan.destination_bucket, ram_quota_mb=record.plan.destination_bucket_ram_quota_mb,
                    )
                finally:
                    client.close()
            if created:
                self._log(record, f"Auto-provisioned destination bucket '{record.plan.destination_bucket}'.")
                time.sleep(2)  # let the bucket warm up before scope/collection calls
            return bool(created)
        except Exception as exc:  # noqa: BLE001
            self._log(record, f"Destination bucket auto-provisioning skipped: {exc}")
            return False

    async def _capture_rollback_state(self, record: MigrationRecord, client: CouchbaseClusterClient) -> None:
        """Record which destination collections existed before the first write,
        so rollback knows what it may drop and where it must restore instead."""
        rs = record.rollback_state
        if rs.captured:
            return
        loader = self._loader(record, client, journal=False)
        existing = await asyncio.to_thread(loader.existing_keyspaces)
        rs.preexisting_collections = sorted(existing)
        rs.captured = True
        targets = {loader.keyspace_for(c) for c in self._included_containers(record)}
        shared = sorted(targets & existing)
        if shared:
            self._log(
                record,
                f"{len(shared)} target collection(s) already exist in the destination ({', '.join(shared[:5])}"
                f"{', ...' if len(shared) > 5 else ''}). "
                + ("Documents overwritten or deleted there are journaled so rollback can restore them."
                   if record.plan.journal_overwrites else "Overwrite journaling is off for this migration."),
            )
        await self._emit(record)

    def _add_owned(self, record: MigrationRecord, keyspaces: list[str] | set[str]) -> None:
        rs = record.rollback_state
        pre = set(rs.preexisting_collections)
        merged = set(rs.owned_collections) | {k for k in keyspaces if k not in pre}
        rs.owned_collections = sorted(merged)

    # -- run ------------------------------------------------------------------

    async def run_migration(self, record: MigrationRecord) -> MigrationRecord:
        if record.phase != MigrationPhase.APPROVED:
            raise ValueError("Migration must be approved before it can start.")

        if await asyncio.to_thread(self._ensure_destination_bucket, record):
            record.rollback_state.bucket_created_by_migration = True

        if uses_xdcr(record):
            return await self._run_couchbase_native_migration(record)
        return await self._run_pipeline(record)

    async def _run_pipeline(self, record: MigrationRecord) -> MigrationRecord:
        """Bulk copy and/or continuous sync for every source except Couchbase
        Enterprise Edition (which uses XDCR). Safe to call again after a restart
        or failure: a finished bulk copy is skipped and sync resumes from the
        persisted checkpoint."""
        plan = record.plan
        dest_client = CouchbaseClusterClient(plan.destination)
        connector = get_connector(plan.source)
        containers = self._included_containers(record)
        passthrough = is_couchbase_source(record)
        continuous = plan.strategy in CONTINUOUS_STRATEGIES
        wants_bulk = _has_bulk_copy(record) and record.sync.bulk_copy_finished_at is None
        holder = {"connector": connector}

        try:
            await self._capture_rollback_state(record, dest_client)
            if passthrough:
                self._note_couchbase_scope(record)
            loader = self._loader(record, dest_client)
            if loader.journal_keyspaces:
                record.rollback_state.journal_keyspace = loader.journal_keyspace

            # cbbackupmgr creates the collections itself during a passthrough bulk
            # copy (and must restore into them by name); otherwise create them now.
            if not (passthrough and wants_bulk):
                self._add_owned(record, await loader.ensure_collections(containers))
                await self._ensure_indexes(record, loader, containers)

            if self._taken_over(record):
                return record
            if continuous and wants_bulk and record.sync.capture_started_at is None:
                record.sync.stage = SyncStage.CAPTURING
                self._log(record, "Recording the source's current change position before the bulk copy starts...")
                await self._emit(record)
                positions = await connector.capture_change_position(containers)
                record.checkpoint.update(positions)
                record.sync.capture_started_at = utcnow()
                record.sync.cdc_method = connector.capabilities.method
                await self._emit(record)

            if wants_bulk:
                if self._taken_over(record):
                    return record
                if continuous:
                    record.sync.stage = SyncStage.BULK_COPY
                record.sync.bulk_copy_started_at = utcnow()
                if passthrough:
                    await self._run_native_full_load(
                        record, plan.source, plan.destination, plan.source.database or "", plan.destination_bucket,
                    )
                    if record.phase == MigrationPhase.FAILED:
                        return record
                    await self._record_native_owned(record, loader)
                    self._add_owned(record, await loader.ensure_collections(containers))
                    await self._ensure_indexes(record, loader, containers)
                else:
                    await self._run_full_load(record, connector, loader, containers)
                record.sync.bulk_copy_finished_at = utcnow()
                await self._emit(record)

            if self._taken_over(record):
                return record
            if continuous:
                return await self._sync_loop(record, holder, loader, containers)

            record.phase = MigrationPhase.VERIFYING
            self._log(record, "Bulk copy complete; verifying document counts...")
            await self._emit(record)
            await self._verify(record, loader, containers, connector)
            record.phase = MigrationPhase.COMPLETE
            self._log(record, "Migration complete.")
            await self._emit(record)
            return record
        except Exception as exc:  # noqa: BLE001
            if record.phase in (MigrationPhase.ROLLING_BACK, MigrationPhase.ROLLED_BACK):
                # A rollback interrupted this run; it owns the phase from here.
                self._log(record, f"Bulk copy / sync stopped for rollback ({exc}).")
                return record
            record.phase = MigrationPhase.FAILED
            record.stats.replication_active = False
            record.error_message = str(exc)
            self._log(record, f"Migration FAILED: {exc}")
            await self._emit(record)
            return record
        finally:
            dest_client.close()
            for conn in {id(connector): connector, id(holder["connector"]): holder["connector"]}.values():
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _ensure_indexes(self, record: MigrationRecord, loader: CouchbaseLoader, containers: list[str]) -> None:
        if not record.plan.create_destination_indexes:
            return
        for warning in await loader.ensure_indexes(containers, cas_index=False):
            self._log(record, f"Index setup: {warning}")

    def _note_couchbase_scope(self, record: MigrationRecord) -> None:
        if not _has_bulk_copy(record):
            return
        excluded = [c.container_name for c in record.plan.containers if not c.include]
        if excluded:
            self._log(
                record,
                f"Note: excluded collection(s) {excluded} are still copied by cbbackupmgr, which copies the "
                "whole bucket; continuous sync only follows the included ones.",
            )

    async def _record_native_owned(self, record: MigrationRecord, loader: CouchbaseLoader) -> None:
        """After cbbackupmgr restored a Couchbase source, note which of the
        source's collections are new in the destination (rollback may drop them)."""
        source_connector = CouchbaseSourceConnector(record.plan.source)
        try:
            pairs = await source_connector.list_scopes_and_collections()
        except Exception as exc:  # noqa: BLE001
            self._log(record, f"Could not list the source's collections to record rollback state: {exc}")
            return
        finally:
            await source_connector.close()
        now = await asyncio.to_thread(loader.existing_keyspaces)
        self._add_owned(record, {f"{s}.{c}" for s, c in pairs} & now)

    async def _run_full_load(
        self, record: MigrationRecord, connector: SourceConnector, loader: CouchbaseLoader, containers: list[str],
    ) -> None:
        if self._taken_over(record):
            raise RuntimeError(f"bulk copy not started (phase is {record.phase.value})")
        record.phase = MigrationPhase.MIGRATING
        self._log(record, f"Starting bulk copy of {len(containers)} container(s)...")
        await self._emit(record)

        record.stats.docs_total = sum(
            c.estimated_count or 0
            for c in (record.validation_report.source_topology.containers if record.validation_report and
                      record.validation_report.source_topology else [])
            if c.name in containers
        )
        record.stats.docs_migrated = 0
        record.stats.docs_failed = 0
        record.stats.bytes_migrated = 0

        monitor = BottleneckMonitor(current_concurrency=record.plan.concurrency)
        current_concurrency = record.plan.concurrency
        throttle_attempts = 0
        start = time.monotonic()
        last_emit = 0.0
        per_container_done: dict[str, int] = {c: 0 for c in containers}

        async def _sink(batch: list[SourceDocument]) -> None:
            nonlocal last_emit, throttle_attempts, current_concurrency
            fresh = await self._fresh(record)
            if fresh is not None and fresh.phase != MigrationPhase.MIGRATING:
                raise RuntimeError(f"bulk copy interrupted (phase changed to {fresh.phase.value})")
            result = await loader.write_batch(batch)
            record.stats.docs_migrated += result.docs_written
            record.stats.docs_failed += result.docs_failed
            record.stats.bytes_migrated += result.bytes_written
            record.rollback_state.journaled_docs += result.journaled
            for key, err in result.failed_keys[:5]:
                record.failed_events.append(FailedEvent(container=batch[0].container, key=key, op="upsert", error=err))
            record.failed_events = record.failed_events[-100:]
            for doc in batch:
                per_container_done[doc.container] = per_container_done.get(doc.container, 0) + 1
            for c, n in per_container_done.items():
                record.stats.per_container.setdefault(c, {})["docs_migrated"] = n

            elapsed = time.monotonic() - start
            record.stats.elapsed_seconds = elapsed
            docs_per_sec = record.stats.docs_migrated / elapsed if elapsed > 0 else 0.0
            record.stats.throughput_docs_per_sec = docs_per_sec
            if record.stats.docs_total:
                remaining = max(record.stats.docs_total - record.stats.docs_migrated, 0)
                record.stats.eta_seconds = remaining / docs_per_sec if docs_per_sec > 0 else None
            if result.latencies_ms:
                record.stats.avg_latency_ms = sum(result.latencies_ms) / len(result.latencies_ms)
            total_done = record.stats.docs_migrated + record.stats.docs_failed
            record.stats.error_rate_pct = (
                (record.stats.docs_failed / total_done * 100.0) if total_done else 0.0
            )

            monitor.observe_throughput(docs_per_sec, elapsed)
            monitor.observe_batch_result(result.docs_written, result.docs_failed)
            for raw in monitor.poll():
                finding = self._record_finding(record, raw, phase="initial_load")
                if (
                    raw.recommended_concurrency is not None
                    and throttle_attempts < MAX_AUTO_THROTTLE_ATTEMPTS
                    and raw.recommended_concurrency < current_concurrency
                ):
                    throttle_attempts += 1
                    old = current_concurrency
                    current_concurrency = max(MIN_AUTO_THROTTLE_CONCURRENCY, raw.recommended_concurrency)
                    loader.set_concurrency(current_concurrency)
                    monitor.current_concurrency = current_concurrency
                    finding.auto_remediated = True
                    finding.suggestion = f"Reduced concurrency from {old} to {current_concurrency}."
                    self._log(record, f"Auto-throttling: reduced concurrency from {old} to {current_concurrency} "
                                       f"({raw.kind.value}, attempt {throttle_attempts}/{MAX_AUTO_THROTTLE_ATTEMPTS}).")
                    await self._remember("bottleneck_auto_remediated", {
                        "migration_name": record.plan.name, "phase": "initial_load",
                        "kind": raw.kind.value, "old_concurrency": old, "new_concurrency": current_concurrency,
                    }, record.migration_id)

            now = time.monotonic()
            if now - last_emit >= EMIT_INTERVAL_S:
                last_emit = now
                await self._emit(record)

        await connector.extract(containers, _sink, batch_size=settings.default_batch_size)
        record.stats.elapsed_seconds = time.monotonic() - start
        record.stats.eta_seconds = 0.0
        self._log(
            record,
            f"Bulk copy complete: {record.stats.docs_migrated} document(s) written, "
            f"{record.stats.docs_failed} failed.",
        )
        await self._emit(record)

    # -- continuous sync ---------------------------------------------------------

    async def _sync_loop(
        self, record: MigrationRecord, holder: dict[str, SourceConnector], loader: CouchbaseLoader,
        containers: list[str],
    ) -> MigrationRecord:
        plan = record.plan
        if self._taken_over(record):
            return record
        record.phase = MigrationPhase.REPLICATING
        record.stats.replication_active = True
        record.sync.cdc_method = holder["connector"].capabilities.method
        if record.sync.stage != SyncStage.DRAINING:
            record.sync.stage = SyncStage.CATCH_UP
        record.error_message = None
        self._log(
            record,
            "Continuous sync started"
            + (" -- applying changes made during the bulk copy first." if record.sync.bulk_copy_finished_at else ".")
        )
        await self._emit(record)

        failures = 0
        backoff = 2.0
        rate = _Rate()
        last_reconcile = time.monotonic()
        batch_size = settings.cdc_batch_size
        max_wait = settings.cdc_batch_max_wait_ms / 1000.0

        while True:
            current = await self._fresh(record)
            if current is None or current.phase != MigrationPhase.REPLICATING:
                return current or record
            record = current
            try:
                stream = holder["connector"].stream_changes(containers, record.checkpoint)
                async for batch in _micro_batches(stream, batch_size, max_wait):
                    current = await self._fresh(record)
                    if current is None or current.phase != MigrationPhase.REPLICATING:
                        return current or record
                    record = current

                    changes = [e for e in batch if e.op in ("upsert", "delete")]
                    if changes:
                        await self._apply_change_batch(record, loader, changes)
                        rate.add(len(changes))
                        failures, backoff = 0, 2.0
                    for event in batch:
                        if event.op == "checkpoint" and event.checkpoint is not None:
                            record.checkpoint[event.container] = event.checkpoint
                            record.sync.last_checkpoint_at = utcnow()

                    caught_up = not changes and any(
                        e.op == "heartbeat" and not e.extra.get("busy") for e in batch
                    )
                    record.stats.mutations_per_sec = rate.per_sec()
                    record.stats.last_replication_poll = utcnow()
                    if caught_up:
                        record.stats.replication_lag_seconds = 0.0
                        if record.sync.stage in (SyncStage.CATCH_UP, SyncStage.RECOVERING):
                            record.sync.stage = SyncStage.STREAMING
                            record.sync.caught_up_at = utcnow()
                            self._log(record, "Caught up with the source; applying changes as they happen.")
                    elif changes and record.sync.stage == SyncStage.RECOVERING:
                        record.sync.stage = SyncStage.CATCH_UP

                    if record.sync.stage == SyncStage.DRAINING:
                        waited = (utcnow() - (record.sync.drain_requested_at or utcnow())).total_seconds()
                        if caught_up or waited >= settings.cutover_drain_timeout_s:
                            if not caught_up:
                                self._log(
                                    record,
                                    f"Cutover: the stream didn't report caught-up within "
                                    f"{settings.cutover_drain_timeout_s}s; verifying anyway.",
                                )
                            return await self._finish_cutover(record, loader, containers, holder["connector"])

                    interval = plan.reconcile_interval_minutes * 60
                    if (
                        interval and record.sync.stage == SyncStage.STREAMING
                        and time.monotonic() - last_reconcile >= interval
                    ):
                        last_reconcile = time.monotonic()
                        runner.spawn(str(record.migration_id), "reconcile", self.run_reconcile(record))

                    await self._emit(record, throttle_s=EMIT_INTERVAL_S)
                raise SourceConnectorError("the change stream ended unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                record.sync.stream_restarts += 1
                record.sync.last_stream_error = str(exc)
                if failures > settings.cdc_max_consecutive_failures:
                    record.phase = MigrationPhase.FAILED
                    record.stats.replication_active = False
                    record.sync.stage = SyncStage.STOPPED
                    record.error_message = (
                        f"Continuous sync stopped after {failures} consecutive failures: {exc}. "
                        "Fix the cause, then resume -- it continues from the last checkpoint."
                    )
                    self._log(record, record.error_message)
                    await self._emit(record)
                    return record
                record.sync.stage = SyncStage.RECOVERING
                self._log(
                    record,
                    f"Change stream error: {exc}. Retrying from the last checkpoint in {backoff:.0f}s "
                    f"(attempt {failures}/{settings.cdc_max_consecutive_failures}).",
                )
                await self._emit(record)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, float(settings.cdc_retry_max_backoff_s))
                try:
                    await holder["connector"].close()
                except Exception:  # noqa: BLE001
                    pass
                holder["connector"] = get_connector(record.plan.source)

    async def _apply_change_batch(
        self, record: MigrationRecord, loader: CouchbaseLoader, events: list[ChangeEvent],
    ) -> None:
        """Write one micro-batch. The last change per key wins (events arrive in
        source order per key). Failed writes are retried; if the destination
        rejects the whole batch, raise so the stream restarts from the last
        checkpoint instead of skipping past the changes."""
        latest: dict[tuple[str, str], ChangeEvent] = {}
        for event in events:
            key = _event_key(event)
            if key:
                latest.pop((event.container, key), None)
                latest[(event.container, key)] = event

        pending_up = [e.document for e in latest.values() if e.op == "upsert" and e.document is not None]
        pending_del: dict[str, list[str]] = {}
        for (container, key), event in latest.items():
            if event.op == "delete":
                pending_del.setdefault(container, []).append(key)

        written = 0
        journaled = 0
        failures: dict[tuple[str, str], tuple[str, str]] = {}
        attempt = 0
        while True:
            failures = {}
            if pending_up:
                result = await loader.write_batch(pending_up)
                written += result.docs_written
                journaled += result.journaled
                by_key = {d.key: d.container for d in pending_up}
                for key, err in result.failed_keys:
                    failures[(by_key.get(key, "?"), key)] = ("upsert", err)
            for container, keys in pending_del.items():
                result = await loader.delete_batch(container, keys)
                written += result.docs_written
                journaled += result.journaled
                for key, err in result.failed_keys:
                    failures[(container, key)] = ("delete", err)
            if not failures or attempt >= settings.cdc_write_retries:
                break
            attempt += 1
            await asyncio.sleep(min(0.25 * (2 ** attempt), 5.0))
            pending_up = [d for d in pending_up if (d.container, d.key) in failures]
            pending_del = {
                c: [k for k in keys if (c, k) in failures] for c, keys in pending_del.items()
            }
            pending_del = {c: ks for c, ks in pending_del.items() if ks}

        if failures and len(failures) == len(latest):
            sample = next(iter(failures.values()))[1]
            raise RuntimeError(f"the destination rejected every write in a batch of {len(latest)} ({sample})")

        for (container, key), (op, err) in failures.items():
            record.failed_events.append(FailedEvent(container=container, key=key, op=op, error=err))
        record.failed_events = record.failed_events[-100:]
        record.stats.docs_failed += len(failures)
        record.stats.mutations_replicated += written
        record.rollback_state.journaled_docs += journaled
        now = utcnow()
        record.sync.last_event_at = now
        for event in events:
            if event.checkpoint is not None:
                record.checkpoint[event.container] = event.checkpoint
        record.sync.last_checkpoint_at = now
        stamps = [e.source_ts for e in events if e.source_ts is not None]
        if stamps:
            record.stats.replication_lag_seconds = max(0.0, (now - max(stamps)).total_seconds())

    async def _finish_cutover(
        self, record: MigrationRecord, loader: CouchbaseLoader, containers: list[str],
        connector: SourceConnector | None,
    ) -> MigrationRecord:
        record.phase = MigrationPhase.VERIFYING
        record.stats.replication_active = False
        record.sync.stage = SyncStage.STOPPED
        self._log(record, "Cutover: change stream drained; verifying document counts...")
        await self._emit(record)
        await self._verify(record, loader, containers, connector)
        record.cutover_at = utcnow()
        record.phase = MigrationPhase.COMPLETE
        self._log(
            record,
            "Cutover complete. Couchbase is now the system of record."
            + (" Fallback sync is available if applications need to move back to the source."
               if self._reverse_sync_supported(record) else ""),
        )
        await self._emit(record)
        await self._remember("cutover_completed", {
            "migration_name": record.plan.name, "source_type": record.plan.source.source_type.value,
            "mutations_replicated": record.stats.mutations_replicated,
        }, record.migration_id)
        return record

    async def stop_replication(self, record: MigrationRecord, perform_cutover: bool) -> MigrationRecord:
        if record.phase != MigrationPhase.REPLICATING:
            raise ValueError(f"Migration is not currently replicating (phase: {record.phase.value}).")

        if uses_xdcr(record):
            return await self._stop_xdcr_replication(record, perform_cutover)

        if not perform_cutover:
            record.phase = MigrationPhase.STOPPED
            record.stats.replication_active = False
            record.sync.stage = SyncStage.STOPPED
            self._log(record, "Continuous sync stopped without cutover. The source remains the system of record; "
                              "resume picks up from the last checkpoint.")
            await self._emit(record)
            return record

        if runner.is_running(str(record.migration_id), "run"):
            record.sync.stage = SyncStage.DRAINING
            record.sync.drain_requested_at = utcnow()
            self._log(
                record,
                "Cutover requested: applying the remaining changes, then verifying. Make sure applications "
                "have stopped writing to the source.",
            )
            await self._emit(record)
            return record

        # No sync task is running (for example auto-resume is disabled): verify directly.
        dest_client = CouchbaseClusterClient(record.plan.destination)
        connector = get_connector(record.plan.source)
        try:
            loader = self._loader(record, dest_client)
            return await self._finish_cutover(record, loader, self._included_containers(record), connector)
        finally:
            dest_client.close()
            await connector.close()

    # -- resume -------------------------------------------------------------------

    def resume_blocker(self, record: MigrationRecord) -> str | None:
        """Why this migration can't be resumed, or None if it can."""
        if record.plan.strategy not in CONTINUOUS_STRATEGIES:
            return "Only continuous migrations can be resumed; start a one-time migration again instead."
        if record.phase not in (MigrationPhase.FAILED, MigrationPhase.STOPPED):
            return f"Resume is for failed or stopped migrations (current phase: {record.phase.value})."
        if record.cutover_at is not None:
            return "This migration was already cut over."
        if record.rollback_state.completed_steps:
            return "A rollback was started for this migration; finish it instead of resuming."
        if not record.approved_at:
            return "This migration was never approved."
        return None

    async def resume(self, record: MigrationRecord) -> MigrationRecord:
        """Continue a stopped/failed (or restart-interrupted) continuous
        migration from its persisted checkpoint. An unfinished bulk copy is run
        again from the start (writes are idempotent)."""
        record.error_message = None
        if uses_xdcr(record):
            return await self._resume_xdcr(record)
        record.phase = (
            MigrationPhase.MIGRATING
            if _has_bulk_copy(record) and record.sync.bulk_copy_finished_at is None
            else MigrationPhase.REPLICATING
        )
        self._log(record, "Resuming from the last checkpoint...")
        await self._emit(record)
        return await self._run_pipeline(record)

    async def resume_after_restart(self, record: MigrationRecord) -> MigrationRecord:
        """Called at startup for a migration a previous process was driving."""
        phase = record.phase
        if phase == MigrationPhase.VERIFYING and not uses_xdcr(record):
            dest_client = CouchbaseClusterClient(record.plan.destination)
            connector = get_connector(record.plan.source)
            try:
                return await self._finish_cutover(
                    record, self._loader(record, dest_client), self._included_containers(record), connector,
                )
            finally:
                dest_client.close()
                await connector.close()
        if uses_xdcr(record):
            return await self._resume_xdcr(record)
        self._log(record, f"Backend restarted while this migration was {phase.value}; resuming from the last checkpoint.")
        await self._emit(record)
        return await self._run_pipeline(record)

    # -- reconciliation -----------------------------------------------------------------

    async def run_reconcile(self, record: MigrationRecord, *, force: bool = False) -> MigrationRecord:
        status = record.sync.reconcile
        if status.running:
            return record
        status.running = True
        status.last_started_at = utcnow()
        self._log(record, "Reconciliation started: comparing source and destination keys to remove deleted documents...")
        await self._emit(record)
        dest_client = CouchbaseClusterClient(record.plan.destination)
        connector = get_connector(record.plan.source)
        try:
            loader = self._loader(record, dest_client)
            outcome = await reconcile(connector, loader, self._included_containers(record), force=force)
            status.deletes_applied_total += outcome.deleted
            status.missing_in_destination = outcome.missing_in_destination
            record.rollback_state.journaled_docs += outcome.journaled
            parts = [f"removed {outcome.deleted} document(s) deleted at the source"]
            if outcome.missing_in_destination:
                parts.append(
                    f"{outcome.missing_in_destination} source key(s) not in the destination yet "
                    "(recent writes still in the stream, or missed changes -- check again after the next run)"
                )
            parts.extend(outcome.notes)
            status.last_result = "; ".join(parts)
            self._log(record, f"Reconciliation finished: {status.last_result}.")
        except Exception as exc:  # noqa: BLE001
            status.last_result = f"failed: {exc}"
            self._log(record, f"Reconciliation failed: {exc}")
        finally:
            status.running = False
            status.last_finished_at = utcnow()
            dest_client.close()
            await connector.close()
            await self._emit(record)
        return record

    # -- verification ------------------------------------------------------------------

    async def _verify(
        self, record: MigrationRecord, loader: CouchbaseLoader, containers: list[str],
        connector: SourceConnector | None,
    ) -> None:
        results: list[ContainerVerification] = []
        passthrough = loader.passthrough
        for container in containers:
            target = loader.keyspace_for(container)
            item = ContainerVerification(container=container, target=target)
            try:
                item.dest_count = await asyncio.to_thread(loader.count_migrated, container)
            except Exception as exc:  # noqa: BLE001
                item.note = f"destination count failed: {exc}"
            if connector is not None and settings.verify_source_counts:
                try:
                    count = 0
                    async for _ in connector.list_keys(container):
                        count += 1
                    item.source_count = count
                except Exception as exc:  # noqa: BLE001
                    item.note = (item.note + "; " if item.note else "") + f"source count unavailable: {exc}"
            if item.source_count is not None and item.dest_count is not None:
                item.matched = item.source_count == item.dest_count
            results.append(item)

        record.verification = results
        record.verified_at = utcnow()
        mismatched = [r for r in results if r.matched is False]
        scope_note = (
            " (whole-collection counts: Couchbase sources are copied without a migration tag)"
            if passthrough else " (documents tagged with this migration)"
        )
        if mismatched:
            detail = ", ".join(f"{r.container}: source {r.source_count} vs destination {r.dest_count}" for r in mismatched[:5])
            self._log(record, f"Verification: {len(mismatched)} container(s) differ{scope_note} -- {detail}.")
        else:
            total = sum(r.dest_count or 0 for r in results)
            self._log(record, f"Verification: {total} document(s) in the destination{scope_note}; "
                              + ("counts match the source." if any(r.matched for r in results) else "source not counted."))

    # -- rollback -------------------------------------------------------------------

    def plan_rollback(self, record: MigrationRecord, req: RollbackRequest | None = None) -> RollbackPlan:
        """Dry run of rollback(): what would be undone, and what can't be."""
        req = req or RollbackRequest(migration_id=record.migration_id)
        rs = record.rollback_state
        phase = record.phase
        steps: list[str] = []
        warnings: list[str] = []
        allowed = phase in _ROLLBACK_PHASES
        if phase == MigrationPhase.FALLBACK_SYNCING:
            warnings.append("Stop the fallback sync before rolling back.")
        elif phase in (MigrationPhase.ROLLING_BACK, MigrationPhase.ROLLED_BACK):
            warnings.append(f"This migration is already {phase.value.replace('_', ' ')}.")
        elif not allowed:
            warnings.append(f"Nothing to roll back in phase '{phase.value}'.")

        needs_confirmation = record.cutover_at is not None and phase != MigrationPhase.FELL_BACK
        if needs_confirmation:
            warnings.insert(0, (
                f"This migration was cut over at {record.cutover_at:%Y-%m-%d %H:%M} UTC. Rolling back removes "
                "what applications have written to these documents in Couchbase since then. If applications "
                "need to move back to the source, use fallback sync instead."
            ))

        native = is_couchbase_source(record)
        targets = {f"{s}.{c}" for s, c in self._target_map(record).values()}
        owned = set(rs.owned_collections) if rs.captured else set()
        shared = sorted(targets - owned) if rs.captured else sorted(targets)

        if phase in (MigrationPhase.MIGRATING, MigrationPhase.REPLICATING, MigrationPhase.VERIFYING):
            steps.append("Stop the running bulk copy / continuous sync.")
        if uses_xdcr(record) and record.checkpoint.get("_xdcr_replication_id"):
            steps.append("Remove the XDCR replication and remote-cluster reference from the source cluster.")
        if not rs.captured and phase != MigrationPhase.APPROVED:
            warnings.append(
                "This migration started before rollback state was recorded, so every target collection is "
                "treated as pre-existing: documents are removed by tag, and no collection is dropped."
            )

        if not native and req.purge_destination_data:
            purge_targets = shared + ([] if req.drop_owned_collections else sorted(owned))
            if purge_targets:
                steps.append(
                    f"Delete the documents this migration wrote to {len(purge_targets)} collection(s): "
                    f"{_short_list(purge_targets)}."
                )
        if record.plan.journal_overwrites and req.restore_overwritten and rs.journal_keyspace:
            steps.append(
                f"Restore the {rs.journaled_docs:,} document(s) this migration overwrote or deleted in "
                "collections that already existed (from the rollback journal)."
                if not native else
                f"Undo the {rs.journaled_docs:,} change(s) continuous sync applied in collections that "
                "already existed (from the rollback journal)."
            )
        elif shared and not record.plan.journal_overwrites:
            warnings.append("Overwrite journaling was off, so documents this migration overwrote can't be restored.")
        if owned:
            if req.drop_owned_collections:
                steps.append(f"Drop the {len(owned)} collection(s) this migration created: {_short_list(sorted(owned))}.")
            elif native:
                warnings.append("Data copied into collections this migration created stays unless you drop them.")
        if native and (_has_bulk_copy(record) or uses_xdcr(record)) and shared:
            tool = "cbbackupmgr and XDCR" if uses_xdcr(record) and _has_bulk_copy(record) else (
                "XDCR" if uses_xdcr(record) else "cbbackupmgr")
            warnings.append(
                f"{tool} wrote documents into {len(shared)} collection(s) that already existed "
                f"({_short_list(shared)}). Those writes aren't journaled and can't be told apart from what was "
                "there before, so they stay."
            )
        if rs.journal_keyspace and req.restore_overwritten:
            steps.append(f"Drop the rollback journal ({rs.journal_keyspace}).")
        if rs.bucket_created_by_migration:
            if req.drop_created_bucket and not record.plan.destination.is_capella:
                steps.append(f"Drop bucket '{record.plan.destination_bucket}', which this migration created.")
            elif req.drop_created_bucket:
                warnings.append(
                    f"Bucket '{record.plan.destination_bucket}' was created in Capella by this migration; drop it "
                    "from the Capella UI if you no longer need it."
                )
            else:
                warnings.append(f"Bucket '{record.plan.destination_bucket}' was created by this migration and is kept.")
        steps.append(
            "Source database: nothing to undo -- the forward migration never writes to it"
            + (" (XDCR setup is removed above)." if uses_xdcr(record) else ".")
        )

        return RollbackPlan(
            migration_id=record.migration_id, phase=phase, allowed=allowed,
            requires_cutover_confirmation=needs_confirmation, steps=steps, warnings=warnings,
        )

    async def rollback(self, record: MigrationRecord, req: RollbackRequest) -> MigrationRecord:
        """Undo this migration's effect on the destination. Idempotent: each
        step is recorded as it completes, so a failed rollback can be run again
        and continues where it stopped."""
        plan = self.plan_rollback(record, req)
        if not plan.allowed:
            raise ValueError(plan.warnings[0] if plan.warnings else "Rollback isn't available in this phase.")
        if plan.requires_cutover_confirmation and not req.confirm_after_cutover:
            raise ValueError("This migration was already cut over; confirm the rollback explicitly to continue.")

        rs = record.rollback_state
        record.phase = MigrationPhase.ROLLING_BACK
        record.stats.replication_active = False
        record.sync.stage = SyncStage.STOPPED
        self._log(record, f"Rolling back (reason: {req.reason})...")
        await self._emit(record)

        # Let a running bulk copy / sync loop notice the phase change and exit
        # before touching the data it was writing.
        for _ in range(120):
            if not runner.is_running(str(record.migration_id), "run"):
                break
            await asyncio.sleep(0.5)

        done = set(rs.completed_steps)
        native = is_couchbase_source(record)
        dest_client = CouchbaseClusterClient(record.plan.destination)
        summary: list[str] = []

        def _mark(step: str) -> None:
            done.add(step)
            rs.completed_steps = sorted(done)

        try:
            if uses_xdcr(record) and "xdcr" not in done and record.checkpoint.get("_xdcr_replication_id"):
                await asyncio.to_thread(self._teardown_xdcr, record)
                _mark("xdcr")

            loader = self._loader(record, dest_client)
            containers = self._included_containers(record)
            owned = set(rs.owned_collections) if rs.captured else set()

            if not native and req.purge_destination_data and "purge" not in done:
                purge = [
                    c for c in containers
                    if loader.keyspace_for(c) not in owned or not req.drop_owned_collections
                ]
                if purge:
                    deleted = await loader.purge_migration(purge)
                    summary.append(f"deleted {deleted:,} migrated document(s)")
                    self._log(record, f"Rollback: deleted {deleted:,} document(s) this migration wrote.")
                _mark("purge")
                await self._emit(record)

            if req.restore_overwritten and "restore_journal" not in done:
                restored, errors = await loader.restore_journal()
                if errors:
                    raise RuntimeError(
                        f"{len(errors)} journaled document(s) could not be restored (e.g. {errors[0]})"
                    )
                if restored:
                    summary.append(f"restored {restored:,} overwritten/deleted document(s)")
                    self._log(record, f"Rollback: restored {restored:,} document(s) from the journal.")
                _mark("restore_journal")
                await self._emit(record)

            if req.drop_owned_collections and "drop_owned" not in done:
                for keyspace in sorted(owned):
                    await loader.drop_keyspace(keyspace)
                if owned:
                    summary.append(f"dropped {len(owned)} collection(s)")
                    self._log(record, f"Rollback: dropped {len(owned)} collection(s) this migration created.")
                _mark("drop_owned")
                await self._emit(record)

            if req.restore_overwritten and rs.journal_keyspace and "drop_journal" not in done:
                await loader.drop_journal()
                _mark("drop_journal")

            if (
                req.drop_created_bucket and rs.bucket_created_by_migration
                and not record.plan.destination.is_capella and "drop_bucket" not in done
            ):
                await asyncio.to_thread(dest_client.drop_bucket, record.plan.destination_bucket)
                summary.append(f"dropped bucket '{record.plan.destination_bucket}'")
                _mark("drop_bucket")

            discard_state(str(record.migration_id))
        except Exception as exc:  # noqa: BLE001
            record.phase = MigrationPhase.FAILED
            record.error_message = f"Rollback stopped: {exc}. Run rollback again to continue from where it stopped."
            self._log(record, record.error_message)
            await self._emit(record)
            return record
        finally:
            dest_client.close()

        record.phase = MigrationPhase.ROLLED_BACK
        record.error_message = None
        rs.last_summary = ("Rolled back: " + ", ".join(summary) + ".") if summary else "Rolled back; nothing needed undoing."
        self._log(record, rs.last_summary + " The source database was never modified by the forward migration.")
        await self._emit(record)
        await self._remember("rollback_completed", {
            "migration_name": record.plan.name, "reason": req.reason, "summary": rs.last_summary,
        }, record.migration_id)
        return record

    # -- post-cutover fallback -------------------------------------------------------

    def _reverse_sync_supported(self, record: MigrationRecord) -> bool:
        try:
            return get_connector(record.plan.source).capabilities.reverse_sync
        except Exception:  # noqa: BLE001
            return False

    def fallback_blocker(self, record: MigrationRecord) -> str | None:
        if record.phase != MigrationPhase.COMPLETE or record.cutover_at is None:
            return "Fallback sync is available once a continuous migration has been cut over."
        if not self._reverse_sync_supported(record):
            return "This source doesn't support writing changes back."
        return None

    async def begin_fallback(self, record: MigrationRecord) -> MigrationRecord:
        blocker = self.fallback_blocker(record)
        if blocker:
            raise ValueError(blocker)
        record.phase = MigrationPhase.FALLBACK_SYNCING
        record.fallback.active = True
        record.fallback.last_error = None
        self._log(record, "Starting fallback sync: recording destination keys, then writing Couchbase changes "
                          "made since cutover back to the source...")
        await self._emit(record)
        return record

    async def run_fallback(self, record: MigrationRecord, *, fresh: bool) -> MigrationRecord:
        dest_client = CouchbaseClusterClient(record.plan.destination)
        connector = get_connector(record.plan.source)
        try:
            loader = self._loader(record, dest_client, journal=False)
            sync = FallbackSync(
                record, connector, loader, self._included_containers(record), log=self._log, emit=self._emit,
            )
            if fresh:
                await sync.start()
                self._log(record, "Fallback sync running: the source is kept current with changes made in Couchbase.")
                await self._emit(record)
            return await sync.run()
        except Exception as exc:  # noqa: BLE001
            record.fallback.last_error = str(exc)
            record.fallback.active = False
            record.phase = MigrationPhase.COMPLETE
            self._log(record, f"Fallback sync couldn't start: {exc}")
            await self._emit(record)
            return record
        finally:
            dest_client.close()
            await connector.close()

    async def stop_fallback(self, record: MigrationRecord, fall_back_to_source: bool) -> MigrationRecord:
        if record.phase != MigrationPhase.FALLBACK_SYNCING:
            raise ValueError("Fallback sync isn't running.")
        record.phase = MigrationPhase.COMPLETE  # stops the running loop
        await self._emit(record)
        for _ in range(60):
            if not runner.is_running(str(record.migration_id), "fallback"):
                break
            await asyncio.sleep(0.5)

        if not fall_back_to_source:
            record.fallback.active = False
            record.fallback.stopped_at = utcnow()
            discard_state(str(record.migration_id))
            self._log(record, "Fallback sync stopped. Couchbase remains the system of record.")
            await self._emit(record)
            return record

        dest_client = CouchbaseClusterClient(record.plan.destination)
        connector = get_connector(record.plan.source)
        try:
            loader = self._loader(record, dest_client, journal=False)
            sync = FallbackSync(
                record, connector, loader, self._included_containers(record), log=self._log, emit=self._emit,
            )
            try:
                await sync.final_drain()
            finally:
                sync.known.close()
        except Exception as exc:  # noqa: BLE001
            record.phase = MigrationPhase.FALLBACK_SYNCING
            record.fallback.last_error = str(exc)
            self._log(record, f"Final fallback drain failed ({exc}); fallback sync is still the way back. Try again.")
            await self._emit(record)
            raise ValueError(f"Final drain failed: {exc}") from exc
        finally:
            dest_client.close()
            await connector.close()

        record.phase = MigrationPhase.FELL_BACK
        record.fallback.active = False
        record.fallback.stopped_at = utcnow()
        discard_state(str(record.migration_id))
        self._log(record, "Fell back: the source is the system of record again and has every change made in "
                          "Couchbase since cutover. Point applications back at the source.")
        await self._emit(record)
        await self._remember("fell_back_to_source", {"migration_name": record.plan.name}, record.migration_id)
        return record

    # -- XDCR lifecycle (Enterprise Edition sources) ------------------------------------

    async def _stop_xdcr_replication(self, record: MigrationRecord, perform_cutover: bool) -> MigrationRecord:
        self._log(
            record,
            "Stopping XDCR replication and performing cutover..." if perform_cutover
            else "Stopping XDCR replication without cutover...",
        )
        record.stats.replication_active = False
        record.sync.stage = SyncStage.STOPPED
        # XDCR runs inside the source cluster and keeps going regardless of this
        # app; it must be torn down explicitly.
        await asyncio.to_thread(self._teardown_xdcr, record)
        if perform_cutover:
            record.phase = MigrationPhase.VERIFYING
            await self._emit(record)
            await self._verify_native(record)
            record.cutover_at = utcnow()
            record.phase = MigrationPhase.COMPLETE
            self._log(record, "Cutover complete. Couchbase is now the system of record.")
        else:
            record.phase = MigrationPhase.STOPPED
            self._log(record, "Replication stopped (XDCR replication removed from the source cluster).")
        await self._emit(record)
        return record

    async def _resume_xdcr(self, record: MigrationRecord) -> MigrationRecord:
        source = record.plan.source
        repl_id = record.checkpoint.get("_xdcr_replication_id")
        alive = False
        if repl_id:
            alive = bool(await asyncio.to_thread(XdcrManager(source).get_progress, repl_id))
        try:
            if not alive:
                self._log(record, "Re-creating the XDCR replication (XDCR re-checks every document and only "
                                  "sends what the destination is missing)...")
                await self._start_xdcr(record, source, record.plan.destination, source.database or "",
                                       record.plan.destination_bucket)
        except Exception as exc:  # noqa: BLE001
            record.phase = MigrationPhase.FAILED
            record.error_message = f"Could not resume XDCR: {exc}"
            self._log(record, record.error_message)
            await self._emit(record)
            return record
        record.phase = MigrationPhase.REPLICATING
        record.stats.replication_active = True
        record.sync.stage = SyncStage.STREAMING
        record.sync.cdc_method = CdcMethod.XDCR
        self._log(record, "XDCR replication is running; monitoring progress.")
        await self._emit(record)
        return await self._monitor_xdcr(record)

    # -- Couchbase-to-Couchbase native path (cbbackupmgr + XDCR) -----------------
    # See this module's docstring and couchbase_native.py's for the full
    # rationale. Kept separate from the generic connector/loader path above
    # rather than threaded through it, since the execution model (subprocess +
    # cluster-side replication config, not a per-document sink) is genuinely
    # different, not just a different connector implementation.

    def _xdcr_ref_name(self, record: MigrationRecord) -> str:
        return f"onboarding-agent-{record.migration_id}"

    async def _run_couchbase_native_migration(self, record: MigrationRecord) -> MigrationRecord:
        source = record.plan.source
        dest = record.plan.destination
        source_bucket = source.database or ""
        dest_bucket = record.plan.destination_bucket

        excluded = [c.container_name for c in record.plan.containers if not c.include]
        remapped = [
            c.container_name for c in record.plan.containers
            if c.include and (c.target_scope_name or c.target_collection_name)
        ]
        if excluded or remapped:
            parts = []
            if excluded:
                parts.append(
                    f"excluded container(s) {excluded} will still be included -- cbbackupmgr backs up/"
                    "restores the whole bucket, not individual collections"
                )
            if remapped:
                parts.append(
                    f"custom target scope/collection names for {remapped} won't be applied -- cbbackupmgr "
                    "restores the source bucket's own scope.collection structure as-is"
                )
            self._log(record, "Note: " + "; ".join(parts) + ".")

        try:
            capture_client = CouchbaseClusterClient(dest)
            try:
                await self._capture_rollback_state(record, capture_client)
                if record.plan.strategy in (MigrationStrategy.FULL_LOAD, MigrationStrategy.FULL_LOAD_AND_CDC):
                    record.sync.bulk_copy_started_at = utcnow()
                    await self._run_native_full_load(record, source, dest, source_bucket, dest_bucket)
                    if record.phase == MigrationPhase.FAILED:
                        return record
                    await self._record_native_owned(record, self._loader(record, capture_client, journal=False))
                    record.sync.bulk_copy_finished_at = utcnow()
            finally:
                capture_client.close()

            if self._taken_over(record):
                return record
            is_continuous = record.plan.strategy in CONTINUOUS_STRATEGIES
            if is_continuous:
                await self._start_xdcr(record, source, dest, source_bucket, dest_bucket)
                record.phase = MigrationPhase.REPLICATING
                record.stats.replication_active = True
                record.sync.stage = SyncStage.STREAMING
                record.sync.cdc_method = CdcMethod.XDCR
                self._log(record, "Starting XDCR-based continuous replication...")
                await self._emit(record)
                return await self._monitor_xdcr(record)

            record.phase = MigrationPhase.VERIFYING
            self._log(record, "Migration transfer complete; verifying document counts on destination...")
            await self._emit(record)
            await self._verify_native(record)
            record.phase = MigrationPhase.COMPLETE
            self._log(record, "Migration complete.")
            await self._emit(record)
            return record
        except Exception as exc:  # noqa: BLE001
            record.phase = MigrationPhase.FAILED
            record.error_message = str(exc)
            self._log(record, f"Migration FAILED: {exc}")
            await self._emit(record)
            return record

    async def _run_native_full_load(self, record: MigrationRecord, source, dest, source_bucket: str, dest_bucket: str) -> None:
        record.phase = MigrationPhase.MIGRATING
        self._log(record, f"Starting full load of bucket '{source_bucket}' via cbbackupmgr...")
        await self._emit(record)

        archive_dir = f"{settings.couchbase_backup_archive_dir}/{record.migration_id}"
        repo = "migration"
        start = time.monotonic()

        # Without this, the dashboard shows nothing moving for the entire
        # backup+restore -- cbbackupmgr's own progress lines were only ever
        # written to record.log_tail via self._log() (console-only), never
        # pushed to record.stats/the websocket the way the generic per-
        # document pipeline's batches do (confirmed as a real gap on
        # 2026-07-30: docs migrated/throughput/the docs-per-sec chart all sat
        # at 0 through a minute-plus backup that was visibly making progress
        # in the container logs). cbbackupmgr already prints a running item
        # count in its own progress lines (see CBBACKUPMGR_PROGRESS_RE) --
        # parse that out and treat it as a live, if approximate, stand-in for
        # docs_migrated/throughput. It's not a per-document guarantee the way
        # the tagged pipeline's counts are (this is cbbackupmgr's own
        # internal bookkeeping across whichever phase -- backup or restore --
        # is currently running), but it's far better than a flat 0 for over a
        # minute.
        last_progress = {"items": 0, "t": start}

        async def log_fn(line: str) -> None:
            self._log(record, line)
            match = CBBACKUPMGR_PROGRESS_RE.search(line)
            if not match:
                return
            items = int(match.group(1).replace(",", ""))
            if items <= last_progress["items"]:
                return  # cbbackupmgr reprints the same figure many times a second
            now = time.monotonic()
            elapsed = now - last_progress["t"]
            if elapsed < EMIT_INTERVAL_S:
                return
            rate = (items - last_progress["items"]) / elapsed if elapsed > 0 else 0.0
            last_progress["items"] = items
            last_progress["t"] = now
            record.stats.docs_migrated = items
            record.stats.throughput_docs_per_sec = rate
            record.stats.elapsed_seconds = now - start
            await self._emit(record)

        runner = CbBackupMgrRunner(log_fn=log_fn)

        # Enumerate the source bucket's own scope/collection structure so restore()
        # can pass cbbackupmgr an explicit, collection-level --map-data mapping --
        # a bucket-level mapping alone doesn't stop restore from rejecting an
        # already-existing destination collection whose internal ID doesn't match
        # the backup manifest's (see couchbase_native.py's restore() docstring).
        collection_pairs: list[tuple[str, str]] | None = None
        source_connector = CouchbaseSourceConnector(source)
        try:
            collection_pairs = await source_connector.list_scopes_and_collections()
        except Exception as exc:  # noqa: BLE001
            self._log(
                record,
                f"Could not enumerate source scopes/collections for an explicit --map-data ({exc}); "
                "falling back to a bucket-level map, which may not avoid a collection-ID-mismatch error.",
            )
        finally:
            await source_connector.close()

        try:
            self._log(record, f"Backing up '{source_bucket}' from the source cluster...")
            await runner.backup(source, archive_dir, repo)
            self._log(record, f"Restoring into destination bucket '{dest_bucket}'...")
            await runner.restore(dest, archive_dir, repo, source_bucket, dest_bucket, collection_pairs=collection_pairs)
        except CouchbaseNativeError as exc:
            record.phase = MigrationPhase.FAILED
            record.error_message = str(exc)
            self._log(record, f"cbbackupmgr migration FAILED: {exc}")
            await self._emit(record)
            return

        record.stats.elapsed_seconds = time.monotonic() - start
        item_count = await asyncio.to_thread(self._dest_bucket_item_count, dest, dest_bucket)
        if item_count is not None:
            record.stats.docs_migrated = item_count
            record.stats.throughput_docs_per_sec = (
                item_count / record.stats.elapsed_seconds if record.stats.elapsed_seconds > 0 else 0.0
            )

        self._log(
            record,
            f"Full load complete via cbbackupmgr ({record.stats.docs_migrated} document(s) in destination "
            f"bucket '{dest_bucket}').",
        )
        await self._emit(record)

    def _dest_bucket_item_count(self, dest, bucket_name: str) -> int | None:
        client = CouchbaseClusterClient(dest)
        try:
            buckets = client.get_buckets()
        except Exception:  # noqa: BLE001
            return None
        finally:
            client.close()
        match = next((b for b in buckets if b.get("name") == bucket_name), None)
        return (match or {}).get("basicStats", {}).get("itemCount")

    async def _start_xdcr(self, record: MigrationRecord, source, dest, source_bucket: str, dest_bucket: str) -> None:
        self._log(record, "Configuring XDCR for continuous replication...")
        xdcr = XdcrManager(source)
        ref_name = self._xdcr_ref_name(record)
        await asyncio.to_thread(xdcr.create_remote_cluster_ref, dest, ref_name)

        # ns_server can return 200 from POST /pools/default/remoteClusters before the
        # new reference is actually visible to createReplication's lookup -- confirmed
        # live on 2026-07-30: a guaranteed-fresh migration (full `docker compose down -v`,
        # no possible stale state) still hit "cannot find remote cluster" immediately
        # after a successful create_remote_cluster_ref() call, so this is a genuine
        # cluster-side propagation gap rather than orphaned app state. Retry
        # create_replication() a few times with a short delay before giving up.
        repl_id: str | None = None
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                repl_id = await asyncio.to_thread(xdcr.create_replication, source_bucket, ref_name, dest_bucket)
                break
            except CouchbaseNativeError as exc:
                is_visibility_race = "remote cluster" in str(exc).lower()
                if not is_visibility_race or attempt == max_attempts:
                    raise
                self._log(
                    record,
                    f"XDCR replication creation attempt {attempt}/{max_attempts} failed "
                    "(remote-cluster reference not yet visible on the source cluster); retrying in 2s...",
                )
                await asyncio.sleep(2.0)
        assert repl_id is not None
        record.checkpoint["_xdcr_ref_name"] = ref_name
        record.checkpoint["_xdcr_replication_id"] = repl_id
        self._log(record, f"XDCR replication started ('{source_bucket}' -> '{dest_bucket}').")

    def _teardown_xdcr(self, record: MigrationRecord) -> None:
        repl_id = record.checkpoint.get("_xdcr_replication_id")
        ref_name = record.checkpoint.get("_xdcr_ref_name")
        if not repl_id and not ref_name:
            return
        xdcr = XdcrManager(record.plan.source)
        try:
            if repl_id:
                xdcr.remove_replication(repl_id)
            if ref_name:
                xdcr.remove_remote_cluster_ref(ref_name)
            self._log(record, "XDCR replication removed from the source cluster.")
        except Exception as exc:  # noqa: BLE001
            self._log(
                record,
                f"Could not fully tear down XDCR: {exc} -- you may need to remove the replication manually "
                "from the source cluster's Couchbase Web Console (XDCR Replications).",
            )

    async def _monitor_xdcr(self, record: MigrationRecord) -> MigrationRecord:
        xdcr = XdcrManager(record.plan.source)
        repl_id = record.checkpoint.get("_xdcr_replication_id")
        start = time.monotonic()
        last_emit = 0.0

        while True:
            fresh = await self.store.get(record.migration_id)
            if fresh is None or fresh.phase != MigrationPhase.REPLICATING:
                return fresh or record
            record = fresh

            if repl_id:
                task = await asyncio.to_thread(xdcr.get_progress, repl_id)
                changes_left = task.get("changesLeft")
                if changes_left is not None:
                    record.stats.changes_left = changes_left
                docs_checked = task.get("docsChecked")
                if docs_checked is not None:
                    record.stats.mutations_replicated = docs_checked
                    elapsed = max(time.monotonic() - start, 0.001)
                    record.stats.mutations_per_sec = docs_checked / elapsed
                record.stats.last_replication_poll = utcnow()

            now = time.monotonic()
            if now - last_emit >= EMIT_INTERVAL_S:
                last_emit = now
                await self._emit(record)

            await asyncio.sleep(settings.xdcr_poll_interval_s)

    async def _verify_native(self, record: MigrationRecord) -> None:
        """Coarser than _verify(): cbbackupmgr/XDCR don't tag documents with a
        migration ID, so this compares whole-bucket item counts rather than a
        count scoped to just what this migration wrote."""
        dest_count = await asyncio.to_thread(
            self._dest_bucket_item_count, record.plan.destination, record.plan.destination_bucket,
        )
        self._log(
            record,
            f"Verification: destination bucket '{record.plan.destination_bucket}' item count = "
            f"{dest_count if dest_count is not None else 'unknown'}. cbbackupmgr/XDCR don't tag documents, "
            "so this is a whole-bucket count rather than a count scoped to just this migration.",
        )


def _short_list(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")
