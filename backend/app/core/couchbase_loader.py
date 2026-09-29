"""
Writes SourceDocument batches into the destination Couchbase bucket, one
scope.collection per source container, and owns everything rollback needs on
the destination side.

Two write modes:
  - tagged (every non-Couchbase source): each document gets a `_migration`
    envelope -- migration id, source container, when it was written, and the
    source-native identity/type hints a post-cutover fallback needs to write it
    back. Verification, reconciliation and rollback select by that tag.
  - passthrough (Couchbase sources): documents keep their original key, scope
    and collection and are written unmodified, mirroring cbbackupmgr/XDCR.

Rollback safety: collections this migration creates are recorded as "owned"
and can simply be dropped. In collections that already existed, a document
the migration is about to overwrite or delete is first copied to a per-migration
pre-image journal (`onboarding_agent`.`rb_<id>`), so rollback can put it back.

Concurrency is a Semaphore-bounded pool of blocking SDK calls run via
asyncio.to_thread; MigrationEngine's auto-throttle adjusts it under backpressure.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from couchbase.exceptions import (
    CasMismatchException,
    CollectionNotFoundException,
    CouchbaseException,
    DocumentExistsException,
    DocumentNotFoundException,
)
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import QueryOptions, RemoveOptions, UpsertOptions

from app.core.connectors.base import SourceDocument
from app.core.connectors.util import approx_size_bytes
from app.core.couchbase_client import CouchbaseClusterClient, sanitize_couchbase_name

logger = logging.getLogger(__name__)

JOURNAL_SCOPE = "onboarding_agent"
MIGRATION_INDEX = "idx_onboarding_migration"
CAS_INDEX = "idx_onboarding_cas"


def journal_collection_name(migration_id: UUID) -> str:
    return f"rb_{migration_id.hex[:16]}"


@dataclass
class LoadResult:
    docs_written: int = 0
    docs_failed: int = 0
    bytes_written: int = 0
    journaled: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failed_keys: list[tuple[str, str]] = field(default_factory=list)  # (key, error)


class CouchbaseLoader:
    """One instance per migration run."""

    def __init__(
        self,
        client: CouchbaseClusterClient,
        bucket: str,
        migration_id: UUID,
        container_target_map: dict[str, tuple[str, str]],
        concurrency: int = 8,
        *,
        passthrough: bool = False,
        journal_keyspaces: set[str] | None = None,
    ):
        """container_target_map: source container -> (scope, collection), already
        sanitized. journal_keyspaces: "scope.collection" targets that existed
        before this migration and so need pre-image journaling."""
        self.client = client
        self.bucket_name = bucket
        self.migration_id = migration_id
        self.container_target_map = container_target_map
        self.passthrough = passthrough
        self.journal_keyspaces = set(journal_keyspaces or ())
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._collections_ready: set[str] = set()
        self._journal_ready = False

    # -- targets -------------------------------------------------------------

    def set_concurrency(self, concurrency: int) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    def _target_for(self, container: str) -> tuple[str, str]:
        return self.container_target_map.get(container, ("_default", sanitize_couchbase_name(container)))

    def keyspace_for(self, container: str) -> str:
        scope, collection = self._target_for(container)
        return f"{scope}.{collection}"

    def _ks(self, scope: str, collection: str) -> str:
        return f"`{self.bucket_name}`.`{scope}`.`{collection}`"

    def _collection(self, scope: str, collection: str):
        return self.client.connect().bucket(self.bucket_name).scope(scope).collection(collection)

    # -- provisioning ----------------------------------------------------------

    def existing_keyspaces(self) -> set[str]:
        """Every scope.collection currently in the destination bucket (empty if
        the bucket doesn't exist yet)."""
        try:
            scopes = self.client.connect().bucket(self.bucket_name).collections().get_all_scopes()
        except CouchbaseException:
            return set()
        return {f"{s.name}.{c.name}" for s in scopes for c in s.collections}

    async def ensure_collections(self, containers: list[str]) -> list[str]:
        """Create target scopes/collections as needed. Returns the keyspaces
        this call actually created."""
        created: list[str] = []
        for container in containers:
            scope, collection = self._target_for(container)
            cache_key = f"{scope}.{collection}"
            if cache_key in self._collections_ready:
                continue
            was_created = await asyncio.to_thread(self._ensure_one, scope, collection)
            if was_created:
                created.append(cache_key)
            self._collections_ready.add(cache_key)
        return created

    def _ensure_one(self, scope: str, collection: str) -> bool:
        before = self.existing_keyspaces()
        self.client.ensure_scope_and_collection(self.bucket_name, scope, collection)
        return f"{scope}.{collection}" not in before

    async def ensure_indexes(self, containers: list[str], *, cas_index: bool) -> list[str]:
        """Create the GSI indexes verification/reconciliation/rollback query with.
        Best effort: returns warnings instead of raising, since a user without
        index-management rights can still migrate (just with slower checks)."""
        warnings: list[str] = []
        cluster = self.client.connect()
        for container in containers:
            scope, collection = self._target_for(container)
            statements = []
            if not self.passthrough:
                statements.append(
                    f"CREATE INDEX IF NOT EXISTS `{MIGRATION_INDEX}` ON {self._ks(scope, collection)}"
                    f"(`_migration`.`migration_id`, `_migration`.`migrated_at`)"
                )
            if cas_index or self.passthrough:
                statements.append(
                    f"CREATE INDEX IF NOT EXISTS `{CAS_INDEX}` ON {self._ks(scope, collection)}(META().cas)"
                )
            for stmt in statements:
                try:
                    await asyncio.to_thread(lambda st=stmt: list(cluster.query(st).rows()))
                except CouchbaseException as exc:
                    warnings.append(f"Could not create an index on {scope}.{collection}: {exc}")
        return warnings

    # -- journal ------------------------------------------------------------------

    @property
    def journal_keyspace(self) -> str:
        return f"{JOURNAL_SCOPE}.{journal_collection_name(self.migration_id)}"

    def _ensure_journal(self) -> None:
        if self._journal_ready:
            return
        collection = journal_collection_name(self.migration_id)
        self.client.ensure_scope_and_collection(self.bucket_name, JOURNAL_SCOPE, collection)
        try:
            list(self.client.connect().query(
                f"CREATE PRIMARY INDEX IF NOT EXISTS ON {self._ks(JOURNAL_SCOPE, collection)}"
            ).rows())
        except CouchbaseException as exc:
            logger.warning("Could not index the rollback journal: %s", exc)
        self._journal_ready = True

    @staticmethod
    def _journal_key(scope: str, collection: str, key: str) -> str:
        raw = f"{scope}::{collection}::{key}"
        if len(raw.encode("utf-8")) <= 240:
            return raw
        return f"h::{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def _journal_pre_image(self, scope: str, collection: str, key: str) -> bool:
        """Copy the current version of `key` into the journal unless it was
        written by this migration or is already journaled (first pre-image
        wins). Returns True if an entry was stored.

        Tagged mode skips keys that don't exist yet -- rollback removes those by
        their `_migration` tag. Passthrough documents carry no tag, so there a
        "did not exist" marker is journaled instead and rollback deletes the key."""
        col = self._collection(scope, collection)
        existed = True
        current: Any = None
        try:
            current = col.get(key).content_as[dict]
        except DocumentNotFoundException:
            existed = False
        except (CouchbaseException, ValueError, TypeError) as exc:
            raise RuntimeError(f"could not read pre-image of {key!r} for the rollback journal: {exc}") from exc
        if not existed and not self.passthrough:
            return False
        envelope = current.get("_migration") if isinstance(current, dict) else None
        if isinstance(envelope, dict) and envelope.get("migration_id") == str(self.migration_id):
            return False
        self._ensure_journal()
        journal = self._collection(JOURNAL_SCOPE, journal_collection_name(self.migration_id))
        try:
            journal.insert(self._journal_key(scope, collection, key), {
                "scope": scope, "collection": collection, "key": key, "existed": existed,
                "pre_image": current, "journaled_at": datetime.utcnow().isoformat(),
            })
        except DocumentExistsException:
            return False
        return True

    # -- writes --------------------------------------------------------------------

    def _envelope(self, doc: SourceDocument) -> dict[str, Any]:
        env: dict[str, Any] = {
            "migration_id": str(self.migration_id),
            "source_container": doc.container,
            "migrated_at": datetime.utcnow().isoformat(),
        }
        if doc.source_key is not None:
            env["source_key"] = doc.source_key
        if doc.types:
            env["types"] = doc.types
        return env

    def _upsert_one(self, doc: SourceDocument) -> tuple[bool, float, int, str | None, bool]:
        scope, collection = self._target_for(doc.container)
        col = self._collection(scope, collection)
        body = dict(doc.body)
        if not self.passthrough:
            body["_migration"] = self._envelope(doc)
        size = approx_size_bytes(body)
        start = time.monotonic()
        journaled = False
        try:
            if f"{scope}.{collection}" in self.journal_keyspaces:
                journaled = self._journal_pre_image(scope, collection, doc.key)
            col.upsert(doc.key, body, UpsertOptions(timeout=None))
            return True, (time.monotonic() - start) * 1000, size, None, journaled
        except Exception as exc:  # noqa: BLE001
            return False, (time.monotonic() - start) * 1000, size, str(exc), journaled

    async def write_batch(self, docs: list[SourceDocument]) -> LoadResult:
        result = LoadResult()

        async def _one(doc: SourceDocument) -> None:
            async with self._semaphore:
                ok, latency_ms, size, err, journaled = await asyncio.to_thread(self._upsert_one, doc)
            result.journaled += int(journaled)
            if ok:
                result.docs_written += 1
                result.bytes_written += size
                result.latencies_ms.append(latency_ms)
            else:
                result.docs_failed += 1
                result.failed_keys.append((doc.key, err or "unknown error"))
                if err and len(result.errors) < 20:
                    result.errors.append(err)

        await asyncio.gather(*(_one(d) for d in docs))
        return result

    def _delete_one(self, container: str, key: str) -> tuple[bool, str | None, bool]:
        scope, collection = self._target_for(container)
        journaled = False
        try:
            if f"{scope}.{collection}" in self.journal_keyspaces:
                journaled = self._journal_pre_image(scope, collection, key)
            self._collection(scope, collection).remove(key)
            return True, None, journaled
        except DocumentNotFoundException:
            # Deleting something that was never migrated (created and removed at the
            # source between the bulk copy and now) isn't an error.
            return True, None, journaled
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), journaled

    async def delete_batch(self, container: str, keys: list[str]) -> LoadResult:
        result = LoadResult()

        async def _one(key: str) -> None:
            async with self._semaphore:
                ok, err, journaled = await asyncio.to_thread(self._delete_one, container, key)
            result.journaled += int(journaled)
            if ok:
                result.docs_written += 1
            else:
                result.docs_failed += 1
                result.failed_keys.append((key, err or "unknown error"))
                if err and len(result.errors) < 20:
                    result.errors.append(err)

        await asyncio.gather(*(_one(k) for k in keys))
        return result

    # -- reads used by verification / reconciliation --------------------------------

    def count_migrated(self, container: str) -> int:
        scope, collection = self._target_for(container)
        cluster = self.client.connect()
        if self.passthrough:
            stmt = f"SELECT RAW COUNT(*) FROM {self._ks(scope, collection)}"
            rows = list(cluster.query(stmt).rows())
        else:
            stmt = (
                f"SELECT RAW COUNT(*) FROM {self._ks(scope, collection)} AS d "
                f"WHERE d._migration.migration_id = $mid"
            )
            rows = list(cluster.query(
                stmt, QueryOptions(named_parameters={"mid": str(self.migration_id)},
                                   scan_consistency=QueryScanConsistency.REQUEST_PLUS),
            ).rows())
        return int(rows[0]) if rows else 0

    async def destination_keys(self, container: str, written_before: datetime) -> AsyncIterator[list[str]]:
        """Keys this migration wrote to `container`'s collection, last written
        before `written_before` (so a document the change stream just created
        isn't mistaken for a stale one), in chunks."""
        scope, collection = self._target_for(container)
        cluster = self.client.connect()
        page = 5000
        if self.passthrough:
            where = "META(d).cas < $before"
            params: dict[str, Any] = {"before": int(written_before.timestamp() * 1_000_000_000)}
        else:
            where = "d._migration.migration_id = $mid AND d._migration.migrated_at < $before"
            params = {"mid": str(self.migration_id), "before": written_before.isoformat()}
        after = ""
        while True:
            stmt = (
                f"SELECT RAW META(d).id FROM {self._ks(scope, collection)} AS d "
                f"WHERE {where} AND META(d).id > $after ORDER BY META(d).id LIMIT {page}"
            )
            query_params = {**params, "after": after}
            rows = await asyncio.to_thread(
                lambda q=stmt, qp=query_params: list(cluster.query(
                    q, QueryOptions(named_parameters=qp, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
                ).rows())
            )
            if not rows:
                return
            yield rows
            if len(rows) < page:
                return
            after = rows[-1]

    def _delete_if_stale_one(self, container: str, key: str, before: datetime) -> tuple[bool, str | None, bool]:
        """Reconciliation delete: remove `key` only if it still hasn't been
        rewritten since `before` (the change stream may have re-created it
        while reconciliation was listing keys). CAS-guarded."""
        scope, collection = self._target_for(container)
        col = self._collection(scope, collection)
        try:
            res = col.get(key)
        except DocumentNotFoundException:
            return True, None, False
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), False
        try:
            doc = res.content_as[dict]
        except Exception:  # noqa: BLE001
            doc = {}
        if self.passthrough:
            fresh = res.cas >= int(before.timestamp() * 1_000_000_000)
        else:
            migrated_at = ((doc or {}).get("_migration") or {}).get("migrated_at") or ""
            fresh = migrated_at >= before.isoformat()
        if fresh:
            return True, None, False
        journaled = False
        try:
            if f"{scope}.{collection}" in self.journal_keyspaces:
                journaled = self._journal_pre_image(scope, collection, key)
            col.remove(key, RemoveOptions(cas=res.cas))
            return True, None, journaled
        except (DocumentNotFoundException, CasMismatchException):
            return True, None, journaled  # changed or gone meanwhile -- leave it to the stream
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), journaled

    async def delete_if_stale(self, container: str, keys: list[str], before: datetime) -> LoadResult:
        result = LoadResult()

        async def _one(key: str) -> None:
            async with self._semaphore:
                ok, err, journaled = await asyncio.to_thread(self._delete_if_stale_one, container, key, before)
            result.journaled += int(journaled)
            if ok:
                result.docs_written += 1
            else:
                result.docs_failed += 1
                result.failed_keys.append((key, err or "unknown error"))

        await asyncio.gather(*(_one(k) for k in keys))
        return result

    # -- rollback ------------------------------------------------------------------

    async def purge_migration(self, containers: list[str]) -> int:
        """Delete every document this migration wrote (tag-selected), for
        rollback of collections that existed before the migration."""
        cluster = self.client.connect()
        deleted = 0
        for container in containers:
            scope, collection = self._target_for(container)
            query = (
                f"DELETE FROM {self._ks(scope, collection)} AS d "
                f"WHERE d._migration.migration_id = $mid RETURNING RAW META(d).id"
            )
            res = await asyncio.to_thread(
                lambda q=query: list(cluster.query(
                    q, QueryOptions(named_parameters={"mid": str(self.migration_id)},
                                    scan_consistency=QueryScanConsistency.REQUEST_PLUS),
                ).rows())
            )
            deleted += len(res)
        return deleted

    async def restore_journal(self) -> tuple[int, list[str]]:
        """Put every journaled pre-image back. Idempotent: restored entries are
        removed from the journal as they're applied."""
        collection = journal_collection_name(self.migration_id)
        if f"{JOURNAL_SCOPE}.{collection}" not in await asyncio.to_thread(self.existing_keyspaces):
            return 0, []
        cluster = self.client.connect()
        journal = self._collection(JOURNAL_SCOPE, collection)
        rows = await asyncio.to_thread(lambda: list(cluster.query(
            f"SELECT META(j).id AS jid, j.`scope`, j.`collection`, j.`key`, j.`existed`, j.pre_image "
            f"FROM {self._ks(JOURNAL_SCOPE, collection)} AS j",
            QueryOptions(scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows()))
        restored = 0
        errors: list[str] = []

        def _restore(row: dict[str, Any]) -> None:
            target = self._collection(row["scope"], row["collection"])
            if row.get("existed") is False or row.get("pre_image") is None:
                try:
                    target.remove(row["key"])
                except DocumentNotFoundException:
                    pass
            else:
                target.upsert(row["key"], row["pre_image"])
            try:
                journal.remove(row["jid"])
            except DocumentNotFoundException:
                pass

        for row in rows:
            try:
                async with self._semaphore:
                    await asyncio.to_thread(_restore, row)
                restored += 1
            except Exception as exc:  # noqa: BLE001
                if len(errors) < 20:
                    errors.append(f"{row.get('scope')}.{row.get('collection')}::{row.get('key')}: {exc}")
        return restored, errors

    async def drop_keyspace(self, keyspace: str) -> None:
        scope, collection = keyspace.split(".", 1)

        def _drop() -> None:
            mgr = self.client.connect().bucket(self.bucket_name).collections()
            try:
                mgr.drop_collection(scope, collection)
            except CollectionNotFoundException:
                pass

        await asyncio.to_thread(_drop)

    async def drop_journal(self) -> None:
        await self.drop_keyspace(self.journal_keyspace)

    async def journal_size(self) -> int:
        collection = journal_collection_name(self.migration_id)
        if f"{JOURNAL_SCOPE}.{collection}" not in await asyncio.to_thread(self.existing_keyspaces):
            return 0
        cluster = self.client.connect()
        try:
            rows = await asyncio.to_thread(lambda: list(cluster.query(
                f"SELECT RAW COUNT(*) FROM {self._ks(JOURNAL_SCOPE, collection)}"
            ).rows()))
            return int(rows[0]) if rows else 0
        except CouchbaseException:
            return -1
