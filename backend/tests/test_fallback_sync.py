"""CAS-polling fallback sync (core/fallback.FallbackSync) against the fakes:
batched reverse writes, retry of rejected keys, watermark handling and delete
propagation. N1QL is replaced by a query stub over FakeLoader.docs."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.core import fallback as fb_mod
from app.core.fallback import FallbackSync
from app.models.enums import MigrationPhase
from app.models.schemas import (
    ContainerMigrationSpec,
    CouchbaseConnectionConfig,
    MigrationPlanCreate,
    MigrationRecord,
    SourceConnectionConfig,
)
from tests.fakes import FakeLoader, FakeSource

KS = "dest._default.items"


def _record() -> MigrationRecord:
    plan = MigrationPlanCreate(
        name="t",
        source=SourceConnectionConfig(label="s", source_type="mongodb", connection_string="mongodb://x"),
        destination=CouchbaseConnectionConfig(label="d", connection_string="couchbase://x", username="u", password="p"),
        destination_bucket="dest", strategy="full_load_and_cdc",
        containers=[ContainerMigrationSpec(container_name="items")],
    )
    record = MigrationRecord(plan=plan)
    record.phase = MigrationPhase.COMPLETE
    record.cutover_at = datetime.utcfromtimestamp(100)
    return record


@pytest.fixture
def sync(monkeypatch, tmp_path):
    monkeypatch.setattr(fb_mod.get_settings(), "migration_state_file", str(tmp_path / "state.json"))
    monkeypatch.setattr(fb_mod.get_settings(), "couchbase_cdc_overlap_s", 0)
    source = FakeSource()
    loader = FakeLoader(None)
    cas: dict[str, int] = {}

    def put(key, body, at):
        loader.docs[(KS, key)] = {**body, "_migration": {"migration_id": "m", "source_key": {"id": key}}}
        cas[key] = at * 1_000_000_000

    def query(self, statement, params):
        rows = [(k, v) for (ks, k), v in loader.docs.items() if ks == KS]
        if "META(d).cas >= $since" in statement:
            rows = [r for r in rows if cas[r[0]] >= params["since"]]
            rows.sort(key=lambda r: cas[r[0]])
            return [{"id": k, "cas": cas[k], "doc": v} for k, v in rows]
        rows = sorted(r for r in rows if r[0] > params["after"])
        return [{"id": k, "sk": v["_migration"].get("source_key"), "t": None} for k, v in rows]

    monkeypatch.setattr(FallbackSync, "_query", query)
    monkeypatch.setattr(FallbackSync, "_ks", lambda self, c: KS)

    async def noop(*a, **k):
        return None

    record = _record()
    s = FallbackSync(record, source, loader, ["items"], log=lambda r, line: None, emit=noop)
    yield s, source, loader, put
    s.known.close(delete_file=True)


async def test_drain_writes_back_in_batches_and_moves_the_watermark(sync):
    s, source, loader, put = sync
    put("a", {"v": 1}, at=50)     # before cutover: must not be written back
    put("b", {"v": 2}, at=150)
    put("c", {"v": 3}, at=160)
    await s.start()
    assert s.record.fallback.method == "cas_poll"
    applied = await s.drain_once()
    assert applied == 2
    assert ("items", "b") in source.rows and ("items", "c") in source.rows and ("items", "a") not in source.rows
    assert s.record.fallback.watermarks[KS] == 160 * 1_000_000_000
    # a second poll re-reads nothing new
    assert await s.drain_once() == 0
    assert len([c for c in source.reverse_calls if c[0] == "upsert"]) == 2


async def test_rejected_keys_are_retried_then_recorded(sync):
    s, source, loader, put = sync
    put("b", {"v": 2}, at=150)
    put("c", {"v": 3}, at=160)
    source.fail_reverse["b"] = 1     # fails once, succeeds on retry
    source.fail_reverse["c"] = -1    # fails forever
    await s.start()
    applied = await s.drain_once()
    assert applied == 1 and ("items", "b") in source.rows
    assert s.record.fallback.errors == 1
    assert [e for e in s.record.failed_events if e.key == "c" and e.op == "reverse_upsert"]
    # the permanently failing key isn't marked as seen, so the next poll's overlap
    # window tries it again
    before = len(source.reverse_calls)
    await s.drain_once()
    assert any(k == "c" for _, k in source.reverse_calls[before:])


async def test_deletes_in_couchbase_propagate_to_the_source(sync):
    s, source, loader, put = sync
    put("b", {"v": 2}, at=150)
    put("gone", {"v": 9}, at=155)
    source.rows[("items", "gone")] = {"v": 9}
    await s.start()
    await s.drain_once()
    del loader.docs[(KS, "gone")]
    assert await s.propagate_deletes() == 1
    assert ("items", "gone") not in source.rows and ("items", "b") in source.rows
    assert s.record.fallback.deletes_applied == 1
    assert await s.propagate_deletes() == 0
