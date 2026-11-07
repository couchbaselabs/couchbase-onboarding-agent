"""Multi-target (bucket/scope/collection) mapping, batched reverse writes, and
the bi-directional cutover -> fall back -> failback -> cutover loop, against
the in-memory fakes."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.core import migration_engine as me
from app.core import runner
from app.core.connectors.base import ReverseOp
from app.core.store import MigrationStore
from app.core.targets import Keyspace, qualify_keyspace, target_map, targets_for
from app.models.enums import MigrationPhase, MigrationStrategy, SyncStage
from app.models.schemas import (
    ContainerMigrationSpec,
    CouchbaseConnectionConfig,
    MigrationPlanCreate,
    MigrationRecord,
    RollbackRequest,
    SourceConnectionConfig,
    TargetKeyspace,
)
from tests.fakes import FakeClient, FakeLoader, FakeSource
from tests.test_engine_sync import start, wait_for

PRIMARY = "dest._default.items"
ARCHIVE = "archive.analytics.items_copy"


def _source(**kw) -> SourceConnectionConfig:
    return SourceConnectionConfig(label="src", source_type="mongodb", connection_string="mongodb://x", **kw)


def _plan(containers, strategy=MigrationStrategy.FULL_LOAD_AND_CDC) -> MigrationPlanCreate:
    return MigrationPlanCreate(
        name="t", source=_source(database="shop"),
        destination=CouchbaseConnectionConfig(label="dst", connection_string="couchbase://x", username="u", password="p"),
        destination_bucket="dest", strategy=strategy, containers=containers, reconcile_interval_minutes=0,
    )


# -- resolution ----------------------------------------------------------------------

def test_targets_default_to_plan_bucket_and_sanitized_names():
    spec = ContainerMigrationSpec(container_name="order items")
    assert targets_for(_source(database="shop"), "dest", spec) == [Keyspace("dest", "shop", "order_items")]


def test_legacy_fields_still_apply_when_targets_empty():
    spec = ContainerMigrationSpec(container_name="items", target_scope_name="s1", target_collection_name="c1")
    assert targets_for(_source(), "dest", spec) == [Keyspace("dest", "s1", "c1")]


def test_multiple_targets_fan_out_and_dedupe():
    spec = ContainerMigrationSpec(container_name="items", targets=[
        TargetKeyspace(),                                   # plan defaults
        TargetKeyspace(bucket="archive", scope="analytics", collection="items_copy"),
        TargetKeyspace(bucket="dest", scope="_default"),     # same as the first after defaults
    ])
    ks = targets_for(_source(), "dest", spec)
    assert ks == [Keyspace("dest", "_default", "items"), Keyspace("archive", "analytics", "items_copy")]
    plan = _plan([spec])
    assert plan.target_buckets == ["dest", "archive"]
    assert target_map(plan.source, "dest", plan.containers)["items"][0].name == "dest.shop.items"


def test_couchbase_sources_keep_scope_collection_but_can_change_bucket():
    src = SourceConnectionConfig(label="cb", source_type="couchbase", connection_string="couchbase://x", database="b")
    spec = ContainerMigrationSpec(container_name="inventory.stock", targets=[
        TargetKeyspace(scope="ignored", collection="ignored"), TargetKeyspace(bucket="second"),
    ])
    assert targets_for(src, "dest", spec) == [
        Keyspace("dest", "inventory", "stock"), Keyspace("second", "inventory", "stock"),
    ]


def test_qualify_keyspace_upgrades_old_records():
    assert qualify_keyspace("_default.items", "dest") == "dest._default.items"
    assert qualify_keyspace("other.s.c", "dest") == "other.s.c"


# -- batched reverse writes ---------------------------------------------------------------

async def test_default_reverse_batch_applies_every_op():
    source = FakeSource()
    source.write("items", "a", {"v": 1})
    result = await source.apply_reverse_batch("items", [
        ReverseOp(op="upsert", key="b", body={"v": 2}, source_key={"id": "b"}),
        ReverseOp(op="delete", key="a", source_key={"id": "a"}),
    ])
    assert result.applied == 2 and result.failed == []
    assert ("items", "b") in source.rows and ("items", "a") not in source.rows


# -- engine: fan-out, verification per target, rollback across buckets -----------------

@pytest.fixture
async def wired_multi(monkeypatch):
    source = FakeSource(containers=("items",))
    state: dict = {}

    def fake_loader(self, record, client, *, journal=True):
        if "loader" not in state:
            state["loader"] = FakeLoader(record.migration_id, targets={"items": [PRIMARY, ARCHIVE]})
        return state["loader"]

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(me.MigrationEngine, "_loader", fake_loader)
    monkeypatch.setattr(me, "CouchbaseClusterClient", lambda cfg: FakeClient())
    monkeypatch.setattr(me, "get_connector", lambda cfg: source)
    monkeypatch.setattr(me.MigrationEngine, "_ensure_destination_buckets", lambda self, record: ["archive"])
    monkeypatch.setattr(me.MigrationEngine, "_remember", _noop)
    yield source, state
    await runner.cancel_all()


def _record(strategy=MigrationStrategy.FULL_LOAD_AND_CDC) -> MigrationRecord:
    spec = ContainerMigrationSpec(container_name="items", targets=[
        TargetKeyspace(), TargetKeyspace(bucket="archive", scope="analytics", collection="items_copy"),
    ])
    record = MigrationRecord(plan=_plan([spec], strategy))
    record.phase = MigrationPhase.APPROVED
    record.approved_at = datetime.utcnow()
    return record


async def test_documents_land_in_every_target_and_verify_per_target(wired_multi):
    source, state = wired_multi
    for i in range(3):
        source.write("items", f"k{i}", {"v": i})
    record = _record(MigrationStrategy.FULL_LOAD)
    engine = await start(record)
    await wait_for(lambda: record.phase == MigrationPhase.COMPLETE)
    docs = state["loader"].docs
    assert docs[(PRIMARY, "k0")]["v"] == 0 and docs[(ARCHIVE, "k0")]["v"] == 0
    assert sorted(v.target for v in record.verification) == sorted([PRIMARY, ARCHIVE])
    assert all(v.matched for v in record.verification)
    assert record.rollback_state.buckets_created_by_migration == ["archive"]
    assert sorted(record.rollback_state.owned_collections) == sorted([PRIMARY, ARCHIVE])

    plan = engine.plan_rollback(record, RollbackRequest(migration_id=record.migration_id, drop_created_bucket=True))
    assert any("'archive'" in s for s in plan.steps)
    await engine.rollback(record, RollbackRequest(migration_id=record.migration_id, drop_created_bucket=True))
    assert record.phase == MigrationPhase.ROLLED_BACK
    assert not docs


# -- bi-directional: cutover -> fallback -> fall back -> failback -> cutover ---------------

class FakeFallbackSync:
    """Stands in for core.fallback.FallbackSync: writes everything the
    destination holds for the containers back to the source once."""

    instances: list = []

    def __init__(self, record, connector, loader, containers, *, log, emit):
        self.record, self.connector, self.loader, self.containers = record, connector, loader, containers
        self.known = type("K", (), {"close": lambda self: None})()
        self.drained = 0
        FakeFallbackSync.instances.append(self)

    async def start(self):
        self.record.fallback.method = "cas_poll"

    async def run(self):
        return self.record

    async def final_drain(self):
        self.drained += 1
        for (ks, key), body in list(self.loader.docs.items()):
            container = ks.split(".")[-1]
            if container in self.containers:
                await self.connector.apply_reverse_batch(container, [
                    ReverseOp(op="upsert", key=key, body={k: v for k, v in body.items() if k != "_migration"},
                              source_key={"id": key}),
                ])


@pytest.fixture
async def wired(monkeypatch):
    source = FakeSource(containers=("items",))
    state: dict = {}

    def fake_loader(self, record, client, *, journal=True):
        if "loader" not in state:
            state["loader"] = FakeLoader(record.migration_id)
        return state["loader"]

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(me.MigrationEngine, "_loader", fake_loader)
    monkeypatch.setattr(me, "CouchbaseClusterClient", lambda cfg: FakeClient())
    monkeypatch.setattr(me, "get_connector", lambda cfg: source)
    monkeypatch.setattr(me.MigrationEngine, "_ensure_destination_buckets", lambda self, record: [])
    monkeypatch.setattr(me.MigrationEngine, "_remember", _noop)
    monkeypatch.setattr(me, "FallbackSync", FakeFallbackSync)
    monkeypatch.setattr(me, "discard_state", lambda mid: None)
    FakeFallbackSync.instances.clear()
    yield source, state
    await runner.cancel_all()


async def test_round_trip_cutover_fallback_failback_cutover(wired):
    source, state = wired
    source.write("items", "a", {"v": 1})
    record = MigrationRecord(plan=_plan([ContainerMigrationSpec(container_name="items")]))
    record.phase = MigrationPhase.APPROVED
    record.approved_at = datetime.utcnow()
    engine = await start(record)
    await wait_for(lambda: record.sync.stage == SyncStage.STREAMING)
    docs = state["loader"].docs

    # 1. cut over: Couchbase is the system of record
    await engine.stop_replication(record, perform_cutover=True)
    await wait_for(lambda: record.phase == MigrationPhase.COMPLETE)
    assert len(record.cutover_history) == 1

    # 2. applications write to Couchbase; fallback sync is started
    docs[(PRIMARY, "written_in_cb")] = {"v": "cb"}
    assert engine.fallback_blocker(record) is None
    await engine.begin_fallback(record)
    await engine.run_fallback(record, fresh=True)
    assert record.phase == MigrationPhase.FALLBACK_SYNCING

    # 3. fall back: the source gets the Couchbase writes, and the forward
    #    position is recorded first so changes at the source aren't lost
    log_len_before = len(source.log)
    await engine.stop_fallback(record, fall_back_to_source=True)
    assert record.phase == MigrationPhase.FELL_BACK
    assert ("items", "written_in_cb") in source.rows
    assert FakeFallbackSync.instances[-1].drained == 1
    assert record.checkpoint["items"] == log_len_before     # captured before the drain
    assert record.sync.capture_started_at is not None
    assert engine.resume_blocker(record) is not None         # not the way back: failback is

    # 4. applications are back on the source and write there
    source.write("items", "written_at_source", {"v": "src"})
    source.delete("items", "a")

    # 5. failback: forward sync resumes from the captured position, no bulk copy
    assert engine.failback_blocker(record) is None
    calls_before = source.calls.count("extract")
    await MigrationStore.instance().save(record)
    runner.spawn(str(record.migration_id), "run", engine.failback(record))
    await wait_for(lambda: (PRIMARY, "written_at_source") in docs)
    await wait_for(lambda: (PRIMARY, "a") not in docs)
    assert record.phase == MigrationPhase.REPLICATING
    assert record.failbacks == 1 and record.cutover_at is None
    assert source.calls.count("extract") == calls_before
    # the drain's own write echoed back through the stream as an identical upsert
    assert docs[(PRIMARY, "written_in_cb")]["v"] == "cb"

    # 6. cut over again: the round trip is complete
    await wait_for(lambda: record.sync.stage == SyncStage.STREAMING)
    await engine.stop_replication(record, perform_cutover=True)
    await wait_for(lambda: record.phase == MigrationPhase.COMPLETE)
    assert len(record.cutover_history) == 2 and record.cutover_at is not None
    assert engine.fallback_blocker(record) is None


async def test_failback_rules():
    engine = me.MigrationEngine()
    record = MigrationRecord(plan=_plan([ContainerMigrationSpec(container_name="items")]))
    assert "fallen back" in engine.failback_blocker(record)
    record.phase = MigrationPhase.FELL_BACK
    assert "No change position" in engine.failback_blocker(record)
    record.sync.capture_started_at = datetime.utcnow()
    assert engine.failback_blocker(record) is None
    once = MigrationRecord(plan=_plan([ContainerMigrationSpec(container_name="items")], MigrationStrategy.FULL_LOAD))
    once.phase = MigrationPhase.FELL_BACK
    assert "continuous" in engine.failback_blocker(once)
