"""In-memory stand-ins for a source connector and the Couchbase loader, so the
engine's sync/rollback logic can be exercised without live databases."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from app.core.connectors.base import CdcCapabilities, ChangeEvent, SourceConnector, SourceDocument, heartbeat
from app.core.couchbase_loader import LoadResult
from app.models.enums import CdcMethod
from app.models.schemas import SourceTopologySnapshot


class FakeSource(SourceConnector):
    """A source with a change log. `rows` is the current state; `log` is an
    append-only list of (seq, op, key, body). capture_change_position() returns
    the current log length, and stream_changes() replays from a position."""

    def __init__(self, config=None, *, containers=("items",), captures_deletes=True):
        self.config = config
        self.containers = list(containers)
        self.rows: dict[tuple[str, str], dict] = {}
        self.log: list[tuple[str, str, str, dict | None]] = []
        self._captures_deletes = captures_deletes
        self.calls: list[str] = []
        self.on_extract_batch = None  # callable run after each extracted batch
        self.fail_stream_times = 0
        self.closed = 0
        self.reverse_calls: list[tuple[str, str]] = []
        self.fail_reverse: dict[str, int] = {}  # key -> failures left (-1: forever)

    # -- helpers for tests
    def write(self, container: str, key: str, body: dict) -> None:
        self.rows[(container, key)] = body
        self.log.append((container, "upsert", key, body))

    def delete(self, container: str, key: str) -> None:
        self.rows.pop((container, key), None)
        self.log.append((container, "delete", key, None))

    # -- connector interface
    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(method=CdcMethod.CHANGE_STREAM, resumable=True,
                               captures_deletes=self._captures_deletes, reverse_sync=True)

    async def test_connection(self) -> SourceTopologySnapshot:  # pragma: no cover - unused
        raise NotImplementedError

    async def extract(self, containers, sink, *, batch_size=500) -> None:
        self.calls.append("extract")
        for container in containers:
            snapshot = [(k, v) for (c, k), v in list(self.rows.items()) if c == container]
            for i in range(0, len(snapshot), 2):
                batch = [SourceDocument(key=k, body=dict(v), container=container, source_key={"id": k})
                         for k, v in snapshot[i:i + 2]]
                await sink(batch)
                if self.on_extract_batch:
                    self.on_extract_batch()

    async def capture_change_position(self, containers) -> dict[str, Any]:
        self.calls.append("capture")
        return {c: len(self.log) for c in containers}

    async def stream_changes(self, containers, checkpoint) -> AsyncIterator[ChangeEvent]:
        self.calls.append("stream")
        if self.fail_stream_times > 0:
            self.fail_stream_times -= 1
            raise RuntimeError("stream dropped")
        pos = min(int(checkpoint.get(c, 0) or 0) for c in containers) if containers else 0
        idle_rounds = 0
        while True:
            if pos < len(self.log):
                container, op, key, body = self.log[pos]
                pos += 1
                idle_rounds = 0
                if container not in containers:
                    continue
                if op == "upsert":
                    yield ChangeEvent(container=container, op="upsert", checkpoint=pos,
                                      document=SourceDocument(key=key, body=dict(body), container=container,
                                                              source_key={"id": key}))
                elif self._captures_deletes:
                    yield ChangeEvent(container=container, op="delete", key=key, checkpoint=pos)
                continue
            idle_rounds += 1
            yield heartbeat()
            await asyncio.sleep(0.01)

    async def list_keys(self, container: str) -> AsyncIterator[str]:
        for (c, k) in list(self.rows):
            if c == container:
                yield k

    async def apply_reverse(self, container, op, key, body, source_key, types=None) -> None:
        self.reverse_calls.append((op, key))
        remaining = self.fail_reverse.get(key, 0)
        if remaining:
            if remaining > 0:
                self.fail_reverse[key] = remaining - 1
            raise RuntimeError(f"source rejected {key}")
        if op == "delete":
            self.rows.pop((container, key), None)
        else:
            self.rows[(container, key)] = dict(body or {})

    async def close(self) -> None:
        self.closed += 1


class FakeClient:
    def close(self) -> None:
        pass

    def drop_bucket(self, name: str) -> None:
        self.dropped_bucket = name


class FakeLoader:
    """Destination as a dict keyed by (bucket.scope.collection, key). `targets`
    maps a container to its keyspaces (default: one, dest._default.<container>)."""

    def __init__(self, migration_id, *, preexisting: dict[tuple[str, str], dict] | None = None,
                 existing_keyspaces: set[str] | None = None, journal_keyspaces: set[str] | None = None,
                 passthrough: bool = False, targets: dict[str, list[str]] | None = None):
        self.migration_id = migration_id
        self.targets = dict(targets or {})
        self.reverse_calls: list[tuple[str, str]] = []
        self.docs: dict[tuple[str, str], dict] = dict(preexisting or {})
        self.keyspaces: set[str] = set(existing_keyspaces or set())
        self.journal_keyspaces = set(journal_keyspaces or set())
        self.journal: dict[tuple[str, str], dict | None] = {}
        self.passthrough = passthrough
        self.client = FakeClient()
        self.bucket_name = "dest"
        self.fail_keys: dict[str, int] = {}   # key -> remaining failures
        self.fail_everything = 0               # number of batches to reject entirely
        self.dropped: list[str] = []

    @property
    def journal_keyspace(self) -> str:
        return "dest.onboarding_agent.rb_test"

    def keyspaces_for(self, container: str) -> list[str]:
        return self.targets.get(container) or [f"dest._default.{container}"]

    def keyspace_for(self, container: str) -> str:
        return self.keyspaces_for(container)[0]

    def all_keyspaces(self) -> set[str]:
        return {ks for c in self.targets for ks in self.keyspaces_for(c)} | {
            ks for (ks, _) in self.docs}

    def existing_keyspaces(self) -> set[str]:
        return set(self.keyspaces)

    async def ensure_collections(self, containers):
        created = []
        for c in containers:
            for ks in self.keyspaces_for(c):
                if ks not in self.keyspaces:
                    self.keyspaces.add(ks)
                    created.append(ks)
        return created

    async def ensure_indexes(self, containers, *, cas_index):
        return []

    def set_concurrency(self, n: int) -> None:
        pass

    def _journal(self, ks: str, key: str) -> bool:
        if ks not in self.journal_keyspaces or (ks, key) in self.journal:
            return False
        current = self.docs.get((ks, key))
        if current is not None and current.get("_migration", {}).get("migration_id") == str(self.migration_id):
            return False
        if current is None and not self.passthrough:
            return False
        self.journal[(ks, key)] = current
        return True

    async def write_batch(self, docs: list[SourceDocument]) -> LoadResult:
        result = LoadResult()
        reject_all = self.fail_everything > 0
        if reject_all:
            self.fail_everything -= 1
        for d in docs:
            if reject_all or self.fail_keys.get(d.key, 0) > 0:
                if not reject_all:
                    self.fail_keys[d.key] -= 1
                result.docs_failed += 1
                result.failed_keys.append((d.key, "temporary failure"))
                continue
            for ks in self.keyspaces_for(d.container):
                result.journaled += int(self._journal(ks, d.key))
                body = dict(d.body)
                if not self.passthrough:
                    body["_migration"] = {"migration_id": str(self.migration_id),
                                          "migrated_at": datetime.utcnow().isoformat()}
                self.docs[(ks, d.key)] = body
            result.docs_written += 1
        return result

    async def delete_batch(self, container: str, keys: list[str]) -> LoadResult:
        result = LoadResult()
        for k in keys:
            for ks in self.keyspaces_for(container):
                result.journaled += int(self._journal(ks, k))
                self.docs.pop((ks, k), None)
            result.docs_written += 1
        return result

    async def delete_if_stale(self, container, keys, before) -> LoadResult:
        return await self.delete_batch(container, keys)

    def count_migrated(self, container: str) -> int:
        return self.count_migrated_in(self.keyspace_for(container))

    def count_migrated_in(self, ks: str) -> int:
        return sum(1 for (k, _), v in self.docs.items()
                   if k == ks and (self.passthrough or v.get("_migration", {}).get("migration_id") == str(self.migration_id)))

    async def destination_keys(self, container, written_before):
        ks = self.keyspace_for(container)
        keys = [key for (k, key), v in self.docs.items()
                if k == ks and v.get("_migration", {}).get("migration_id") == str(self.migration_id)]
        if keys:
            yield keys

    async def purge_migration(self, containers, keyspaces=None) -> int:
        n = 0
        for c in containers:
            for ks in self.keyspaces_for(c):
                if keyspaces is not None and ks not in keyspaces:
                    continue
                for key in [key for (k, key), v in self.docs.items()
                            if k == ks and v.get("_migration", {}).get("migration_id") == str(self.migration_id)]:
                    del self.docs[(ks, key)]
                    n += 1
        return n

    async def restore_journal(self):
        restored = 0
        for (ks, key), pre in list(self.journal.items()):
            if pre is None:
                self.docs.pop((ks, key), None)
            else:
                self.docs[(ks, key)] = pre
            del self.journal[(ks, key)]
            restored += 1
        return restored, []

    async def drop_keyspace(self, keyspace: str) -> None:
        self.dropped.append(keyspace)
        self.keyspaces.discard(keyspace)
        for k in [k for k in self.docs if k[0] == keyspace]:
            del self.docs[k]

    async def drop_journal(self) -> None:
        self.dropped.append(self.journal_keyspace)
