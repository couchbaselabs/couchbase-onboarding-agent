"""Bulk copy + continuous sync, resilience, cutover and rollback, exercised
against in-memory fakes (tests/fakes.py)."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app.core import migration_engine as me
from app.core import runner
from app.core.store import MigrationStore
from app.models.enums import MigrationPhase, MigrationStrategy, SyncStage
from app.models.schemas import (
    ContainerMigrationSpec,
    CouchbaseConnectionConfig,
    MigrationPlanCreate,
    MigrationRecord,
    RollbackRequest,
    SourceConnectionConfig,
)
from tests.fakes import FakeClient, FakeLoader, FakeSource

ITEMS = "_default.items"


def make_record(strategy: MigrationStrategy, containers=("items",), **plan_kwargs) -> MigrationRecord:
    plan = MigrationPlanCreate(
        name="test",
        source=SourceConnectionConfig(
            label="src", source_type="mongodb", connection_string="mongodb://x", password="s3cret",
        ),
        destination=CouchbaseConnectionConfig(
            label="dst", connection_string="couchbase://x", username="u", password="p",
        ),
        destination_bucket="dest",
        strategy=strategy,
        containers=[ContainerMigrationSpec(container_name=c) for c in containers],
        reconcile_interval_minutes=0,
        **plan_kwargs,
    )
    record = MigrationRecord(plan=plan)
    record.phase = MigrationPhase.APPROVED
    record.approved_at = datetime.utcnow()
    return record


async def wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
async def wired(monkeypatch):
    source = FakeSource(containers=("items", "fresh"))
    state: dict = {"existing": set(), "preexisting_docs": {}}

    def fake_loader(self, record, client, *, journal=True):
        if "loader" not in state:
            state["loader"] = FakeLoader(
                record.migration_id, preexisting=state["preexisting_docs"], existing_keyspaces=state["existing"],
            )
        loader = state["loader"]
        rs = record.rollback_state
        loader.journal_keyspaces = (
            set(rs.preexisting_collections) if journal and record.plan.journal_overwrites and rs.captured else set()
        )
        return loader

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(me.MigrationEngine, "_loader", fake_loader)
    monkeypatch.setattr(me, "CouchbaseClusterClient", lambda cfg: FakeClient())
    monkeypatch.setattr(me, "get_connector", lambda cfg: source)
    monkeypatch.setattr(me.MigrationEngine, "_ensure_destination_bucket", lambda self, record: False)
    monkeypatch.setattr(me.MigrationEngine, "_remember", _noop)
    yield source, state
    await runner.cancel_all()


async def start(record: MigrationRecord) -> me.MigrationEngine:
    await MigrationStore.instance().save(record)
    engine = me.MigrationEngine()
    runner.spawn(str(record.migration_id), "run", engine.run_migration(record))
    return engine


async def test_changes_during_bulk_copy_are_replayed(wired):
    source, state = wired
    for i in range(6):
        source.write("items", f"k{i}", {"v": i})

    batches = {"n": 0}

    def during_copy():
        batches["n"] += 1
        if batches["n"] == 1:
            source.write("items", "k0", {"v": "updated during copy"})
            source.write("items", "new", {"v": "created during copy"})
            source.delete("items", "k5")

    source.on_extract_batch = during_copy
    record = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    engine = await start(record)
    await wait_for(lambda: record.sync.stage == SyncStage.STREAMING)

    assert source.calls.index("capture") < source.calls.index("extract") < source.calls.index("stream")
    docs = state["loader"].docs
    assert docs[(ITEMS, "k0")]["v"] == "updated during copy"
    assert docs[(ITEMS, "new")]["v"] == "created during copy"
    assert (ITEMS, "k5") not in docs

    source.write("items", "k1", {"v": "live"})
    await wait_for(lambda: docs[(ITEMS, "k1")]["v"] == "live")

    updated = await engine.stop_replication(record, perform_cutover=True)
    assert updated.sync.stage == SyncStage.DRAINING
    await wait_for(lambda: record.phase == MigrationPhase.COMPLETE)
    assert record.cutover_at is not None
    assert record.verification and all(v.matched for v in record.verification)
    assert record.checkpoint["items"] == len(source.log)


async def test_stream_errors_retry_from_checkpoint(wired):
    source, state = wired
    source.write("items", "a", {"v": 1})
    source.fail_stream_times = 2
    record = make_record(MigrationStrategy.CDC_LIVE)
    await start(record)
    await wait_for(lambda: (ITEMS, "a") in state.get("loader", FakeLoader(None)).docs, timeout=15)
    assert record.sync.stream_restarts == 2
    await wait_for(lambda: record.sync.stage == SyncStage.STREAMING)
    record.phase = MigrationPhase.STOPPED


async def test_rejected_batch_is_not_skipped(wired):
    source, state = wired
    source.write("items", "a", {"v": 1})
    record = make_record(MigrationStrategy.CDC_LIVE)
    await MigrationStore.instance().save(record)
    loader = FakeLoader(record.migration_id)
    loader.fail_everything = 2  # the first attempt and its in-place retry
    state["loader"] = loader
    engine = me.MigrationEngine()
    runner.spawn(str(record.migration_id), "run", engine.run_migration(record))
    await wait_for(lambda: (ITEMS, "a") in loader.docs, timeout=15)
    assert record.sync.stream_restarts == 1
    assert record.failed_events == []
    record.phase = MigrationPhase.STOPPED


async def test_isolated_failures_are_recorded_not_blocking(wired):
    source, state = wired
    record = make_record(MigrationStrategy.CDC_LIVE)
    await MigrationStore.instance().save(record)
    loader = FakeLoader(record.migration_id)
    loader.fail_keys["bad"] = 99
    state["loader"] = loader
    source.write("items", "good1", {"v": 1})
    source.write("items", "bad", {"v": 2})
    source.write("items", "good2", {"v": 3})
    runner.spawn(str(record.migration_id), "run", me.MigrationEngine().run_migration(record))
    await wait_for(lambda: record.failed_events)
    assert (ITEMS, "good1") in loader.docs and (ITEMS, "good2") in loader.docs
    assert [e.key for e in record.failed_events] == ["bad"]
    assert record.stats.docs_failed == 1
    record.phase = MigrationPhase.STOPPED


async def test_rollback_restores_overwritten_and_drops_created(wired):
    source, state = wired
    state["existing"] = {ITEMS}
    state["preexisting_docs"] = {(ITEMS, "k1"): {"v": "original"}, (ITEMS, "untouched"): {"v": "keep"}}
    source.write("items", "k1", {"v": "from source"})
    source.write("items", "k2", {"v": "from source"})
    source.write("fresh", "f1", {"v": "from source"})
    record = make_record(MigrationStrategy.FULL_LOAD, containers=("items", "fresh"))
    engine = await start(record)
    await wait_for(lambda: record.phase == MigrationPhase.COMPLETE)
    docs = state["loader"].docs
    assert docs[(ITEMS, "k1")]["v"] == "from source"
    assert record.rollback_state.owned_collections == ["_default.fresh"]

    plan = engine.plan_rollback(record)
    assert plan.allowed and not plan.requires_cutover_confirmation
    assert any("Drop the 1 collection" in s for s in plan.steps)

    await engine.rollback(record, RollbackRequest(migration_id=record.migration_id))
    assert record.phase == MigrationPhase.ROLLED_BACK
    assert docs[(ITEMS, "k1")] == {"v": "original"}
    assert docs[(ITEMS, "untouched")] == {"v": "keep"}
    assert (ITEMS, "k2") not in docs
    assert "_default.fresh" in state["loader"].dropped


async def test_rollback_after_cutover_needs_confirmation(wired):
    record = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    record.phase = MigrationPhase.COMPLETE
    record.cutover_at = datetime.utcnow()
    await MigrationStore.instance().save(record)
    engine = me.MigrationEngine()
    assert engine.plan_rollback(record).requires_cutover_confirmation
    with pytest.raises(ValueError):
        await engine.rollback(record, RollbackRequest(migration_id=record.migration_id))


async def test_resume_rules():
    engine = me.MigrationEngine()
    record = make_record(MigrationStrategy.FULL_LOAD)
    record.phase = MigrationPhase.FAILED
    assert "continuous" in engine.resume_blocker(record)
    record = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    record.phase = MigrationPhase.FAILED
    assert engine.resume_blocker(record) is None
    record.cutover_at = datetime.utcnow()
    assert engine.resume_blocker(record) is not None


async def test_rollback_during_bulk_copy_keeps_rollback_phase(wired):
    source, state = wired
    for i in range(20):
        source.write("items", f"k{i}", {"v": i})
    gate = asyncio.Event()

    def slow():
        # Let the rollback start while the bulk copy is mid-way.
        gate.set()

    source.on_extract_batch = slow
    record = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    engine = await start(record)
    await asyncio.wait_for(gate.wait(), 5)
    await engine.rollback(record, RollbackRequest(migration_id=record.migration_id))
    await asyncio.sleep(0.2)
    assert record.phase == MigrationPhase.ROLLED_BACK
    assert not [k for k in state["loader"].docs if k[0] == ITEMS]


async def test_startup_does_not_resume_records_from_older_versions(monkeypatch):
    from app import main as app_main
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_resume_on_startup", True)
    spawned: list[str] = []
    monkeypatch.setattr(app_main.runner, "spawn", lambda mid, kind, coro: (coro.close(), spawned.append(kind)))

    old = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    old.phase = MigrationPhase.REPLICATING          # left "running" by a pre-1.1 backend
    new = make_record(MigrationStrategy.FULL_LOAD_AND_CDC)
    new.phase = MigrationPhase.REPLICATING
    new.rollback_state.captured = True
    new.sync.stage = SyncStage.STREAMING
    for r in (old, new):
        await MigrationStore.instance().save(r)

    await app_main._recover_interrupted_migrations()
    assert spawned == ["run"]
    reloaded = await MigrationStore.instance().get(old.migration_id)
    assert reloaded.phase == MigrationPhase.FAILED and "Resume" in reloaded.error_message
