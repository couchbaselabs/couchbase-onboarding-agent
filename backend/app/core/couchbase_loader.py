"""
Writes SourceDocument batches into the destination, one or more
bucket.scope.collection keyspaces per source container, and owns everything
rollback needs on the destination side.

Targets: a container maps to a list of keyspaces (core/targets.py). The first
is the primary copy (counts, reconciliation and fallback read it); every target
receives the same upserts and deletes.

Two write modes:
  - tagged (every non-Couchbase source): each document gets a `_migration`
    envelope -- migration id, source container, when it was written, and the
    source-native identity/type hints a post-cutover fallback needs to write it
    back. Verification, reconciliation and rollback select by that tag.
  - passthrough (Couchbase sources): documents keep their original key, scope
    and collection and are written unmodified, mirroring cbbackupmgr/XDCR.

Write path: documents are grouped per keyspace and written with the SDK's
upsert_multi/remove_multi, which the C++ core pipelines across KV connections
-- far fewer round trips than one blocking call per document. Keyspaces that
need pre-image journaling fall back to the per-document path, since the
pre-image read must happen before each overwrite.

Rollback safety: collections this migration creates are recorded as "owned"
and can simply be dropped. In collections that already existed, a document
the migration is about to overwrite or delete is first copied to a per-migration
pre-image journal (`<default bucket>`.`onboarding_agent`.`rb_<id>`), so rollback
can put it back.

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
from app.core.targets import Keyspace, parse_keyspace, qualify_keyspace

logger = logging.getLogger(__name__)

JOURNAL_SCOPE = "onboarding_agent"
MIGRATION_INDEX = "idx_onboarding_migration"
CAS_INDEX = "idx_onboarding_cas"
# Documents per upsert_multi/remove_multi call. Large enough to amortize the
# call, small enough that one slow chunk doesn't hold the semaphore for long.
MULTI_CHUNK = 100


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
        container_target_map: dict[str, list[Keyspace]],
        concurrency: int = 8,
        *,
        passthrough: bool = False,
        journal_keyspaces: set[str] | None = None,
    ):
        """bucket: the plan's default bucket (also where the rollback journal
        lives). container_target_map: source container -> keyspaces, already
        sanitized, primary first. journal_keyspaces: "bucket.scope.collection"
        targets that existed before this migration and so need pre-image
        journaling ("scope.collection" entries from older records are accepted
        and taken to be in the default bucket)."""
        self.client = client
        self.bucket_name = bucket
        self.migration_id = migration_id
        self.container_target_map = container_target_map
        self.passthrough = passthrough
        self.journal_keyspaces = {qualify_keyspace(k, bucket) for k in (journal_keyspaces or ())}
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._collections_ready: set[str] = set()
        self._journal_ready = False

    # -- targets -------------------------------------------------------------

    def set_concurrency(self, concurrency: int) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

    def targets_for(self, container: str) -> list[Keyspace]:
        found = self.container_target_map.get(container)
        if found:
            return list(found)
        return [Keyspace(self.bucket_name, "_default", sanitize_couchbase_name(container))]

    def _target_for(self, container: str) -> Keyspace:
        return self.targets_for(container)[0]

    def keyspace_for(self, container: str) -> str:
        """Primary keyspace, "bucket.scope.collection"."""
        return self._target_for(container).name

    def keyspaces_for(self, container: str) -> list[str]:
        return [ks.name for ks in self.targets_for(container)]

    def all_keyspaces(self) -> set[str]:
        return {ks.name for lst in self.container_target_map.values() for ks in lst}

    def buckets(self) -> list[str]:
        out = [self.bucket_name]
        for lst in self.container_target_map.values():
            for ks in lst:
                if ks.bucket not in out:
                    out.append(ks.bucket)
        return out

    def _parse(self, keyspace: str) -> Keyspace:
        return parse_keyspace(keyspace, self.bucket_name)

    def _collection(self, ks: Keyspace):
        return self.client.connect().bucket(ks.bucket).scope(ks.scope).collection(ks.collection)

    # -- provisioning ----------------------------------------------------------

    def existing_keyspaces(self) -> set[str]:
        """Every bucket.scope.collection currently in the destination buckets
        this migration touches (a bucket that doesn't exist yet contributes
        nothing)."""
        out: set[str] = set()
        cluster = self.client.connect()
        for bucket in self.buckets():
            try:
                scopes = cluster.bucket(bucket).collections().get_all_scopes()
            except CouchbaseException:
                continue
            out.update(f"{bucket}.{s.name}.{c.name}" for s in scopes for c in s.collections)
        return out

    async def ensure_collections(self, containers: list[str]) -> list[str]:
        """Create target scopes/collections as needed. Returns the keyspaces
        this call actually created."""
        created: list[str] = []
        for container in containers:
            for ks in self.targets_for(container):
                if ks.name in self._collections_ready:
                    continue
                was_created = await asyncio.to_thread(self._ensure_one, ks)
                if was_created:
                    created.append(ks.name)
                self._collections_ready.add(ks.name)
        return created

    def _ensure_one(self, ks: Keyspace) -> bool:
        before = self.existing_keyspaces()
        self.client.ensure_scope_and_collection(ks.bucket, ks.scope, ks.collection)
        return ks.name not in before

    async def ensure_indexes(self, containers: list[str], *, cas_index: bool) -> list[str]:
        """Create the GSI indexes verification/reconciliation/rollback query with.
        Best effort: returns warnings instead of raising, since a user without
        index-management rights can still migrate (just with slower checks)."""
        warnings: list[str] = []
        cluster = self.client.connect()
        done: set[str] = set()
        for container in containers:
            for ks in self.targets_for(container):
                if ks.name in done:
                    continue
                done.add(ks.name)
                statements = []
                if not self.passthrough:
                    statements.append(
                        f"CREATE INDEX IF NOT EXISTS `{MIGRATION_INDEX}` ON {ks.n1ql}"
                        f"(`_migration`.`migration_id`, `_migration`.`migrated_at`)"
                    )
                if cas_index or self.passthrough:
                    statements.append(f"CREATE INDEX IF NOT EXISTS `{CAS_INDEX}` ON {ks.n1ql}(META().cas)")
                for stmt in statements:
                    try:
                        await asyncio.to_thread(lambda st=stmt: list(cluster.query(st).rows()))
                    except CouchbaseException as exc:
                        warnings.append(f"Could not create an index on {ks.name}: {exc}")
        return warnings

    # -- journal ------------------------------------------------------------------

    @property
    def journal_keyspace(self) -> str:
        return f"{self.bucket_name}.{JOURNAL_SCOPE}.{journal_collection_name(self.migration_id)}"

    def _journal_ks(self) -> Keyspace:
        return Keyspace(self.bucket_name, JOURNAL_SCOPE, journal_collection_name(self.migration_id))

    def _ensure_journal(self) -> None:
        if self._journal_ready:
            return
        ks = self._journal_ks()
        self.client.ensure_scope_and_collection(ks.bucket, ks.scope, ks.collection)
        try:
            list(self.client.connect().query(f"CREATE PRIMARY INDEX IF NOT EXISTS ON {ks.n1ql}").rows())
        except CouchbaseException as exc:
            logger.warning("Could not index the rollback journal: %s", exc)
        self._journal_ready = True

    @staticmethod
    def _journal_key(ks: Keyspace, key: str) -> str:
        raw = f"{ks.bucket}::{ks.scope}::{ks.collection}::{key}"
        if len(raw.encode("utf-8")) <= 240:
            return raw
        return f"h::{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def _journal_pre_image(self, ks: Keyspace, key: str) -> bool:
        """Copy the current version of `key` into the journal unless it was
        written by this migration or is already journaled (first pre-image
        wins). Returns True if an entry was stored.

        Tagged mode skips keys that don't exist yet -- rollback removes those by
        their `_migration` tag. Passthrough documents carry no tag, so there a
        "did not exist" marker is journaled instead and rollback deletes the key."""
        col = self._collection(ks)
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
        journal = self._collection(self._journal_ks())
        try:
            journal.insert(self._journal_key(ks, key), {
                "bucket": ks.bucket, "scope": ks.scope, "collection": ks.collection, "key": key,
                "existed": existed, "pre_image": current, "journaled_at": datetime.utcnow().isoformat(),
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

    def _body_for(self, doc: SourceDocument) -> dict[str, Any]:
        body = dict(doc.body)
        if not self.passthrough:
            body["_migration"] = self._envelope(doc)
        return body

    def _upsert_group(self, ks: Keyspace, docs: list[SourceDocument]) -> tuple[dict[str, str | None], float, int]:
        """Write `docs` to one keyspace. Returns (key -> error or None,
        latency_ms for the group, journaled count)."""
        col = self._collection(ks)
        outcome: dict[str, str | None] = {}
        journaled = 0
        start = time.monotonic()
        if ks.name in self.journal_keyspaces:
            # Pre-image must be read before each overwrite: per-document path.
            for doc in docs:
                try:
                    journaled += int(self._journal_pre_image(ks, doc.key))
                    col.upsert(doc.key, self._body_for(doc), UpsertOptions(timeout=None))
                    outcome[doc.key] = None
                except Exception as exc:  # noqa: BLE001
                    outcome[doc.key] = str(exc)
            return outcome, (time.monotonic() - start) * 1000, journaled
        payload = {doc.key: self._body_for(doc) for doc in docs}
        try:
            res = col.upsert_multi(payload, return_exceptions=True)
            errors = res.exceptions if not res.all_ok else {}
            for key in payload:
                exc = errors.get(key)
                outcome[key] = None if exc is None else str(exc)
        except Exception as exc:  # noqa: BLE001
            for key in payload:
                outcome[key] = str(exc)
        return outcome, (time.monotonic() - start) * 1000, journaled

    async def write_batch(self, docs: list[SourceDocument]) -> LoadResult:
        """Upsert every document into each of its container's target keyspaces.
        A document counts as written when every target accepted it."""
        result = LoadResult()
        if not docs:
            return result
        # (keyspace, chunk of docs): group by target, then chunk.
        groups: list[tuple[Keyspace, list[SourceDocument]]] = []
        by_ks: dict[Keyspace, list[SourceDocument]] = {}
        for doc in docs:
            for ks in self.targets_for(doc.container):
                by_ks.setdefault(ks, []).append(doc)
        for ks, lst in by_ks.items():
            for i in range(0, len(lst), MULTI_CHUNK):
                groups.append((ks, lst[i:i + MULTI_CHUNK]))

        failures: dict[tuple[str, str], str] = {}

        async def _one(ks: Keyspace, chunk: list[SourceDocument]) -> None:
            async with self._semaphore:
                outcome, latency_ms, journaled = await asyncio.to_thread(self._upsert_group, ks, chunk)
            result.journaled += journaled
            result.latencies_ms.append(latency_ms / max(len(chunk), 1))
            by_key = {d.key: d.container for d in chunk}
            for key, err in outcome.items():
                ident = (by_key.get(key, ""), key)
                if err is not None and ident not in failures:
                    failures[ident] = f"{ks.name}: {err}"

        await asyncio.gather(*(_one(ks, chunk) for ks, chunk in groups))
        for doc in docs:
            err = failures.get((doc.container, doc.key))
            if err is None:
                result.docs_written += 1
                result.bytes_written += approx_size_bytes(doc.body) * len(self.targets_for(doc.container))
            else:
                result.docs_failed += 1
                result.failed_keys.append((doc.key, err))
                if len(result.errors) < 20:
                    result.errors.append(err)
        return result

    def _delete_group(self, ks: Keyspace, keys: list[str]) -> tuple[dict[str, str | None], int]:
        col = self._collection(ks)
        outcome: dict[str, str | None] = {}
        journaled = 0
        if ks.name in self.journal_keyspaces:
            for key in keys:
                try:
                    journaled += int(self._journal_pre_image(ks, key))
                    col.remove(key)
                    outcome[key] = None
                except DocumentNotFoundException:
                    outcome[key] = None
                except Exception as exc:  # noqa: BLE001
                    outcome[key] = str(exc)
            return outcome, journaled
        try:
            res = col.remove_multi(keys, return_exceptions=True)
            errors = res.exceptions if not res.all_ok else {}
            for key in keys:
                exc = errors.get(key)
                # Deleting something that was never migrated (created and removed at
                # the source between the bulk copy and now) isn't an error.
                outcome[key] = None if exc is None or isinstance(exc, DocumentNotFoundException) else str(exc)
        except Exception as exc:  # noqa: BLE001
            for key in keys:
                outcome[key] = str(exc)
        return outcome, journaled

    async def delete_batch(self, container: str, keys: list[str]) -> LoadResult:
        result = LoadResult()
        if not keys:
            return result
        failures: dict[str, str] = {}

        async def _one(ks: Keyspace, chunk: list[str]) -> None:
            async with self._semaphore:
                outcome, journaled = await asyncio.to_thread(self._delete_group, ks, chunk)
            result.journaled += journaled
            for key, err in outcome.items():
                if err is not None and key not in failures:
                    failures[key] = f"{ks.name}: {err}"

        jobs = [
            _one(ks, keys[i:i + MULTI_CHUNK])
            for ks in self.targets_for(container) for i in range(0, len(keys), MULTI_CHUNK)
        ]
        await asyncio.gather(*jobs)
        for key in keys:
            err = failures.get(key)
            if err is None:
                result.docs_written += 1
            else:
                result.docs_failed += 1
                result.failed_keys.append((key, err))
                if len(result.errors) < 20:
                    result.errors.append(err)
        return result

    # -- reads used by verification / reconciliation --------------------------------

    def count_migrated(self, container: str) -> int:
        return self.count_migrated_in(self.keyspace_for(container))

    def count_migrated_in(self, keyspace: str) -> int:
        ks = self._parse(keyspace)
        cluster = self.client.connect()
        if self.passthrough:
            rows = list(cluster.query(f"SELECT RAW COUNT(*) FROM {ks.n1ql}").rows())
        else:
            stmt = f"SELECT RAW COUNT(*) FROM {ks.n1ql} AS d WHERE d._migration.migration_id = $mid"
            rows = list(cluster.query(
                stmt, QueryOptions(named_parameters={"mid": str(self.migration_id)},
                                   scan_consistency=QueryScanConsistency.REQUEST_PLUS),
            ).rows())
        return int(rows[0]) if rows else 0

    async def destination_keys(self, container: str, written_before: datetime) -> AsyncIterator[list[str]]:
        """Keys this migration wrote to `container`'s primary keyspace, last
        written before `written_before` (so a document the change stream just
        created isn't mistaken for a stale one), in chunks."""
        ks = self._target_for(container)
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
                f"SELECT RAW META(d).id FROM {ks.n1ql} AS d "
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

    def _delete_if_stale_one(self, ks: Keyspace, key: str, before: datetime) -> tuple[bool, str | None, bool]:
        """Reconciliation delete: remove `key` only if it still hasn't been
        rewritten since `before` (the change stream may have re-created it
        while reconciliation was listing keys). CAS-guarded."""
        col = self._collection(ks)
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
            if ks.name in self.journal_keyspaces:
                journaled = self._journal_pre_image(ks, key)
            col.remove(key, RemoveOptions(cas=res.cas))
            return True, None, journaled
        except (DocumentNotFoundException, CasMismatchException):
            return True, None, journaled  # changed or gone meanwhile -- leave it to the stream
        except Exception as exc:  # noqa: BLE001
            return False, str(exc), journaled

    async def delete_if_stale(self, container: str, keys: list[str], before: datetime) -> LoadResult:
        """Applied to every target of the container, so secondary copies don't
        keep documents the primary dropped."""
        result = LoadResult()
        failures: dict[str, str] = {}

        async def _one(ks: Keyspace, key: str) -> None:
            async with self._semaphore:
                ok, err, journaled = await asyncio.to_thread(self._delete_if_stale_one, ks, key, before)
            result.journaled += int(journaled)
            if not ok and key not in failures:
                failures[key] = f"{ks.name}: {err or 'unknown error'}"

        await asyncio.gather(*(_one(ks, k) for ks in self.targets_for(container) for k in keys))
        for key in keys:
            if key in failures:
                result.docs_failed += 1
                result.failed_keys.append((key, failures[key]))
            else:
                result.docs_written += 1
        return result

    # -- rollback ------------------------------------------------------------------

    async def purge_migration(self, containers: list[str], keyspaces: set[str] | None = None) -> int:
        """Delete every document this migration wrote (tag-selected) from the
        containers' targets (restricted to `keyspaces` when given), for rollback
        of collections that existed before the migration."""
        cluster = self.client.connect()
        deleted = 0
        done: set[str] = set()
        for container in containers:
            for ks in self.targets_for(container):
                if ks.name in done or (keyspaces is not None and ks.name not in keyspaces):
                    continue
                done.add(ks.name)
                query = (
                    f"DELETE FROM {ks.n1ql} AS d WHERE d._migration.migration_id = $mid RETURNING RAW META(d).id"
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
        jks = self._journal_ks()
        if jks.name not in await asyncio.to_thread(self.existing_keyspaces):
            return 0, []
        cluster = self.client.connect()
        journal = self._collection(jks)
        rows = await asyncio.to_thread(lambda: list(cluster.query(
            f"SELECT META(j).id AS jid, j.`bucket`, j.`scope`, j.`collection`, j.`key`, j.`existed`, j.pre_image "
            f"FROM {jks.n1ql} AS j",
            QueryOptions(scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows()))
        restored = 0
        errors: list[str] = []

        def _restore(row: dict[str, Any]) -> None:
            target = self._collection(Keyspace(row.get("bucket") or self.bucket_name, row["scope"], row["collection"]))
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
                    errors.append(f"{row.get('bucket') or self.bucket_name}.{row.get('scope')}.{row.get('collection')}"
                                  f"::{row.get('key')}: {exc}")
        return restored, errors

    async def drop_keyspace(self, keyspace: str) -> None:
        ks = self._parse(keyspace)

        def _drop() -> None:
            mgr = self.client.connect().bucket(ks.bucket).collections()
            try:
                mgr.drop_collection(ks.scope, ks.collection)
            except CollectionNotFoundException:
                pass

        await asyncio.to_thread(_drop)

    async def drop_journal(self) -> None:
        await self.drop_keyspace(self.journal_keyspace)

    async def journal_size(self) -> int:
        jks = self._journal_ks()
        if jks.name not in await asyncio.to_thread(self.existing_keyspaces):
            return 0
        cluster = self.client.connect()
        try:
            rows = await asyncio.to_thread(lambda: list(cluster.query(f"SELECT RAW COUNT(*) FROM {jks.n1ql}").rows()))
            return int(rows[0]) if rows else 0
        except CouchbaseException:
            return -1
