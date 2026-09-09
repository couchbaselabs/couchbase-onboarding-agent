"""
Writes SourceDocument batches (produced by a SourceConnector) into a Couchbase
bucket, one scope.collection per source container. This is the "restore" half of
the pipeline -- the sibling couchbase-migration-agent project's equivalent is
cbbackupmgr restore; here it's the Couchbase Python SDK doing per-document upserts,
since there's no equivalent bulk-load binary that understands five different
source formats.

Concurrency is a plain asyncio.Semaphore-bounded pool of blocking SDK calls run via
asyncio.to_thread -- the Couchbase Python SDK's synchronous Cluster/Collection API
is not itself async, matching how the sibling project's AgentMemoryStore and
CouchbaseClusterClient use it. MigrationEngine's auto-throttle loop (see
bottleneck_detector.py) adjusts this pool's concurrency in response to detected
backpressure, the same lever the sibling project uses for cbbackupmgr's --threads.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from couchbase.exceptions import CouchbaseException
from couchbase.options import UpsertOptions

from app.core.connectors.base import SourceDocument
from app.core.connectors.util import approx_size_bytes
from app.core.couchbase_client import CouchbaseClusterClient, sanitize_couchbase_name

logger = logging.getLogger(__name__)


@dataclass
class LoadResult:
    docs_written: int = 0
    docs_failed: int = 0
    bytes_written: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CouchbaseLoader:
    """One instance per migration. Resolves each source container to a
    scope.collection (creating it if needed), and writes documents there."""

    def __init__(
        self,
        client: CouchbaseClusterClient,
        bucket: str,
        migration_id: UUID,
        container_target_map: dict[str, tuple[str, str]],
        concurrency: int = 8,
    ):
        """container_target_map: source container name -> (scope_name, collection_name),
        already sanitized (see couchbase_client.sanitize_couchbase_name)."""
        self.client = client
        self.bucket_name = bucket
        self.migration_id = migration_id
        self.container_target_map = container_target_map
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._collections_ready: set[str] = set()

    def set_concurrency(self, concurrency: int) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    def _target_for(self, container: str) -> tuple[str, str]:
        return self.container_target_map.get(container, ("_default", sanitize_couchbase_name(container)))

    async def ensure_collections(self, containers: list[str]) -> None:
        for container in containers:
            scope, collection = self._target_for(container)
            cache_key = f"{scope}.{collection}"
            if cache_key in self._collections_ready:
                continue
            await asyncio.to_thread(
                self.client.ensure_scope_and_collection, self.bucket_name, scope, collection
            )
            self._collections_ready.add(cache_key)

    def _upsert_one(self, doc: SourceDocument) -> tuple[bool, float, int, str | None]:
        scope, collection = self._target_for(doc.container)
        cluster = self.client.connect()
        col = cluster.bucket(self.bucket_name).scope(scope).collection(collection)
        body = dict(doc.body)
        body["_migration"] = {
            "migration_id": str(self.migration_id),
            "source_container": doc.container,
            "migrated_at": datetime.utcnow().isoformat(),
        }
        size = approx_size_bytes(body)
        start = time.monotonic()
        try:
            col.upsert(doc.key, body, UpsertOptions(timeout=None))
            return True, (time.monotonic() - start) * 1000, size, None
        except CouchbaseException as exc:
            return False, (time.monotonic() - start) * 1000, size, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, (time.monotonic() - start) * 1000, size, str(exc)

    async def write_batch(self, docs: list[SourceDocument]) -> LoadResult:
        result = LoadResult()

        async def _one(doc: SourceDocument) -> None:
            async with self._semaphore:
                ok, latency_ms, size, err = await asyncio.to_thread(self._upsert_one, doc)
            if ok:
                result.docs_written += 1
                result.bytes_written += size
                result.latencies_ms.append(latency_ms)
            else:
                result.docs_failed += 1
                if err and len(result.errors) < 20:
                    result.errors.append(err)

        await asyncio.gather(*(_one(d) for d in docs))
        return result

    async def delete_batch(self, container: str, keys: list[str]) -> LoadResult:
        result = LoadResult()

        def _delete_one(key: str) -> tuple[bool, str | None]:
            scope, collection = self._target_for(container)
            cluster = self.client.connect()
            col = cluster.bucket(self.bucket_name).scope(scope).collection(collection)
            try:
                col.remove(key)
                return True, None
            except CouchbaseException as exc:
                # A delete for a key that was never migrated (e.g. arrived and left
                # the source between introspection and CDC start) isn't an error.
                if "document not found" in str(exc).lower():
                    return True, None
                return False, str(exc)

        async def _one(key: str) -> None:
            async with self._semaphore:
                ok, err = await asyncio.to_thread(_delete_one, key)
            if ok:
                result.docs_written += 1
            else:
                result.docs_failed += 1
                if err and len(result.errors) < 20:
                    result.errors.append(err)

        await asyncio.gather(*(_one(k) for k in keys))
        return result

    async def purge_migration(self, containers: list[str]) -> int:
        """Delete every document this migration wrote, via N1QL, for rollback. Scans
        by the `_migration.migration_id` tag every upsert carries (see _upsert_one)
        rather than dropping the whole collection, since a target collection can be
        shared across migrations if the user pointed two source containers at the
        same destination collection name."""
        cluster = self.client.connect()
        deleted = 0
        for container in containers:
            scope, collection = self._target_for(container)
            query = (
                f"DELETE FROM `{self.bucket_name}`.`{scope}`.`{collection}` AS d "
                f"WHERE d._migration.migration_id = $migration_id RETURNING META(d).id"
            )
            try:
                res = await asyncio.to_thread(
                    cluster.query, query, migration_id=str(self.migration_id)
                )
                deleted += sum(1 for _ in res)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Purge failed for %s.%s: %s", scope, collection, exc)
        return deleted
