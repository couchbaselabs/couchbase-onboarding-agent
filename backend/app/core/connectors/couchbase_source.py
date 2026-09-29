"""
Couchbase Server source connector -- for migrating OUT of one Couchbase
cluster (Community Edition, Enterprise Edition, or Capella) INTO this app's
usual destination, another Couchbase Server Enterprise Edition cluster or
Capella. Source and destination happen to be the same product here.

Unlike every other source in this app, actually MOVING documents for a
Couchbase source does NOT go through this connector: migration_engine.py
detects models.enums.COUCHBASE_SOURCE_TYPES and routes to
core/couchbase_native.py instead, which uses Couchbase's own native tools
(cbbackupmgr for one-time/full-load, XDCR for continuous replication) rather
than this app's generic per-document extract/upsert pipeline -- see that
module's docstring for the full rationale (and what it trades away: the
read-only-against-source guarantee, and per-migration-tagged verify/rollback).

This connector's job is narrower: test_connection() for the wizard's "Test &
introspect source" step and validator.py's connectivity/edition/CDC-support
checks. It:
  - Connects via the SDK, and lists every scope.collection in the configured
    bucket via the SDK's CollectionManager (cluster.bucket(b).collections()
    .get_all_scopes()) rather than raw REST calls -- this works identically
    against self-managed Couchbase Server and Capella, whereas the classic
    per-node REST admin API (used below only for self-managed version/edition
    detection) is not something Capella exposes to external callers the same
    way (see couchbase_native.py's docstring for why the destination side of
    this app already has a separate CapellaClient for the same reason).
  - Best-effort counts each collection via a cheap `SELECT RAW COUNT(1)` N1QL
    query, which does need at least a primary index -- see the note surfaced
    per-collection below if one's missing (this connector won't create one:
    still read-only for introspection purposes).
  - Continuous sync: an Enterprise Edition source uses XDCR (configured on
    the source by couchbase_native.py). Community Edition has no XDCR and a
    Capella source's XDCR is managed through Capella itself, so for those two
    this connector provides stream_changes(): it polls N1QL for documents whose
    META().cas is past a watermark (CAS is a hybrid-logical-clock timestamp, so
    it orders writes in time). Keys, scopes and collections are kept as-is.
    Deletes don't show up in that query; the engine's periodic reconciliation
    (list_keys() against the destination) removes them. Polling needs the Query
    service and an index; `CREATE INDEX ... ON <collection>(META().cas)` makes
    each poll cheap (a primary index works, but scans).

extract() is intentionally NOT a real implementation -- bulk copy for every
Couchbase source is cbbackupmgr -- and raises immediately if ever called.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import requests
from couchbase.cluster import Cluster
from couchbase.exceptions import CouchbaseException, DocumentNotFoundException
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import ClusterOptions, QueryOptions

from app.config import get_settings

from app.core.tls import requests_verify, sdk_authenticator, sdk_connection_string
from app.core.connectors.base import (
    CdcCapabilities,
    ChangeEvent,
    SourceConnector,
    SourceConnectorError,
    SourceDocument,
    heartbeat,
)
from app.core.connectors.util import SeenRecently
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)

_NO_INDEX_MARKERS = ("No index available", "primary index", "index_not_found")


class CouchbaseSourceConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._cluster: Cluster | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def uses_xdcr(self) -> bool:
        return self.config.source_type == SourceType.COUCHBASE_ENTERPRISE

    @property
    def capabilities(self) -> CdcCapabilities:
        if self.uses_xdcr:
            return CdcCapabilities(method=CdcMethod.XDCR, resumable=True, captures_deletes=True, reverse_sync=True)
        return CdcCapabilities(method=CdcMethod.CAS_POLL, resumable=True, captures_deletes=False, reverse_sync=True)

    @property
    def _is_capella(self) -> bool:
        return self.config.source_type == SourceType.COUCHBASE_CAPELLA

    # -- connection -------------------------------------------------------------

    def _bucket(self) -> str:
        if not self.config.database:
            raise SourceConnectorError("Couchbase source requires a bucket name (set 'database').")
        return self.config.database

    def _connect(self, timeout_s: int = 15) -> Cluster:
        if self._cluster is not None:
            return self._cluster
        if not self.config.connection_string:
            raise SourceConnectorError("Couchbase source requires a connection string, e.g. couchbase://host1,host2.")
        try:
            auth = sdk_authenticator(self.config.username or "", self.config.password or "", self.config)
            opts = ClusterOptions(auth, network="external" if self.config.couchbase_external_network else "auto")
            opts.apply_profile("wan_development")
            cluster = Cluster(sdk_connection_string(self.config.connection_string, self.config), opts)
            cluster.wait_until_ready(timedelta(seconds=timeout_s))
        except CouchbaseException as exc:
            raise SourceConnectorError(f"Could not connect to Couchbase source ({self.config.label}): {exc}") from exc
        self._cluster = cluster
        return cluster

    async def close(self) -> None:
        if self._cluster is not None:
            await asyncio.to_thread(self._cluster.close)
            self._cluster = None

    # -- REST management API (self-managed only -- see class docstring) ---------

    def _mgmt_base_url(self) -> str:
        host = (
            (self.config.connection_string or "")
            .replace("couchbases://", "").replace("couchbase://", "")
            .split(",")[0].split("/")[0]
        )
        scheme = "https" if self.config.use_tls else "http"
        port = 18091 if self.config.use_tls else 8091
        return f"{scheme}://{host}:{port}"

    def _rest_get(self, path: str) -> dict[str, Any]:
        url = f"{self._mgmt_base_url()}{path}"
        try:
            resp = requests.get(
                url, auth=(self.config.username or "", self.config.password or ""),
                verify=requests_verify(self.config), timeout=15,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                raise SourceConnectorError(
                    f"{self.config.label}: authentication failed (401) calling {url}. Check username/password."
                ) from exc
            raise SourceConnectorError(f"{self.config.label}: REST call to {url} failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise SourceConnectorError(f"{self.config.label}: could not reach {url} ({exc}).") from exc
        return resp.json()

    # -- XDCR pre-flight (used by validator.py's XDCR_VBUCKET_COMPAT check) -----

    async def get_vbucket_count(self) -> int | None:
        """Returns this source bucket's vBucket count, or None if it can't be
        determined (e.g. a Capella source -- supports_cdc is already False for
        those, so continuous/hybrid replication and this check don't apply).

        XDCR refuses to replicate between buckets with different vBucket
        counts, and a bucket's vBucket count can never be changed after it's
        created (confirmed against a live cluster on 2026-07-30: restore
        failed outright with "The number of vbuckets in source cluster, 1024,
        and target cluster, 128, does not match. This configuration is not
        supported." after a full cbbackupmgr backup+restore had already run --
        exactly the kind of wasted time this pre-flight check exists to avoid).
        """
        if self._is_capella:
            return None
        return await asyncio.to_thread(self._get_vbucket_count_sync)

    def _get_vbucket_count_sync(self) -> int | None:
        bucket = self._bucket()
        try:
            data = self._rest_get(f"/pools/default/buckets/{bucket}")
        except SourceConnectorError:
            return None
        vbucket_map = (data.get("vBucketServerMap") or {}).get("vBucketMap")
        return len(vbucket_map) if vbucket_map else None

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        snapshot = await asyncio.to_thread(self._test_connection_sync)
        return self.describe_capabilities(snapshot)

    def _test_connection_sync(self) -> SourceTopologySnapshot:
        cluster = self._connect()
        bucket = self._bucket()

        if self._is_capella:
            server_version = None
            edition = "Couchbase Capella"
        else:
            pools = self._rest_get("/pools/default")
            nodes = pools.get("nodes", [])
            raw_version = (nodes[0].get("version") if nodes else None) or "unknown"
            server_version = raw_version.split("-")[0]
            lowered = raw_version.lower()
            if "enterprise" in lowered:
                edition = "Enterprise Edition"
            elif "community" in lowered:
                edition = "Community Edition"
            else:
                edition = "edition unconfirmed"

        scopes = cluster.bucket(bucket).collections().get_all_scopes()
        pairs: list[tuple[str, str]] = [
            (scope.name, coll.name) for scope in scopes if scope.name != "_system" for coll in scope.collections
        ]

        containers: list[SourceContainerStats] = []
        total_count = 0
        for scope_name, coll_name in pairs:
            container_name = _container_name(scope_name, coll_name)
            keyspace = f"`{bucket}`.`{scope_name}`.`{coll_name}`"
            notes: list[str] = []
            count: int | None = None
            sample_fields: list[str] = []
            try:
                rows = list(cluster.query(f"SELECT RAW COUNT(1) FROM {keyspace}"))
                count = int(rows[0]) if rows else 0
            except CouchbaseException as exc:
                if _looks_like_missing_index(exc):
                    notes.append(
                        "No queryable index on this collection -- N1QL requires at least a primary "
                        "index. Create one (e.g. `CREATE PRIMARY INDEX ON " + keyspace + "`) before "
                        "migrating; this connector won't create one itself."
                    )
                else:
                    notes.append(f"Could not estimate document count: {exc}")

            if count is not None:
                try:
                    sample_rows = list(cluster.query(f"SELECT t.* FROM {keyspace} AS t LIMIT 1"))
                    if sample_rows:
                        sample_fields = list(dict(sample_rows[0]).keys())[:25]
                except CouchbaseException:
                    pass

            containers.append(SourceContainerStats(
                name=container_name, estimated_count=count, estimated_size_bytes=None,
                sample_fields=sample_fields, notes=notes,
            ))
            total_count += count or 0

        queryable = all(c.estimated_count is not None for c in containers)
        if self.uses_xdcr:
            supports_cdc = True
            cdc_notes = (
                "Continuous replication uses Couchbase's built-in XDCR, configured directly on the "
                "source cluster (see core/couchbase_native.py)."
            )
        elif queryable:
            supports_cdc = True
            cdc_notes = (
                "Continuous sync polls the Query service for documents changed since the last poll "
                "(by META().cas). For large collections create a CAS index first, e.g. "
                "`CREATE INDEX idx_onboarding_cas ON <bucket>.<scope>.<collection>(META().cas)`. "
                "Deletes reach the destination through periodic reconciliation."
            )
        else:
            supports_cdc = False
            cdc_notes = (
                "Continuous sync from this edition polls the Query service, and at least one "
                "collection has no usable index. Create a primary or META().cas index on each "
                "collection (see the per-collection notes), then test the connection again."
            )

        return SourceTopologySnapshot(
            source_type=self.config.source_type,
            server_version=server_version,
            server_edition=f"Couchbase {(server_version + ' ') if server_version else ''}({edition})",
            containers=containers,
            total_estimated_count=total_count or None,
            total_estimated_size_bytes=None,
            supports_cdc=supports_cdc,
            cdc_notes=cdc_notes,
        )

    # -- scope/collection enumeration (used by couchbase_native.py to build an
    # explicit --map-data restore mapping -- see that module) ------------------

    async def list_scopes_and_collections(self) -> list[tuple[str, str]]:
        """Returns every (scope, collection) pair in the configured bucket,
        excluding the internal `_system` scope. cbbackupmgr's restore matches
        collections against the backup manifest by internal ID, not name; if
        the destination collection already exists (e.g. a retried migration,
        or one independently created on the destination), its ID won't match
        and restore refuses with "...a manual remap using '--map-data' is
        required" even though the names line up. Passing an explicit
        bucket.scope.collection=bucket.scope.collection mapping for every
        collection switches restore to name-based matching and sidesteps the
        ID check entirely -- confirmed against a live cluster on 2026-07-30."""
        return await asyncio.to_thread(self._list_scopes_and_collections_sync)

    def _list_scopes_and_collections_sync(self) -> list[tuple[str, str]]:
        cluster = self._connect()
        bucket = self._bucket()
        scopes = cluster.bucket(bucket).collections().get_all_scopes()
        return [
            (scope.name, coll.name) for scope in scopes if scope.name != "_system" for coll in scope.collections
        ]

    # -- extraction -------------------------------------------------------------

    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        raise SourceConnectorError(
            "CouchbaseSourceConnector.extract() should never be called -- migration_engine.py routes "
            "every Couchbase-family source through core/couchbase_native.py's cbbackupmgr/XDCR pipeline "
            "instead. Seeing this error means that routing didn't happen; it's a bug in this app, not a "
            "problem with your source cluster."
        )

    # -- continuous sync (CE / Capella), reconciliation, fallback ------------------

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        overlap_ns = get_settings().couchbase_cdc_overlap_s * 1_000_000_000
        now_ns = time.time_ns()
        return {c: {"cas": now_ns - overlap_ns} for c in containers}


    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:  # type: ignore[override]
        settings = get_settings()
        cluster = await asyncio.to_thread(self._connect)
        bucket = self._bucket()
        overlap_ns = settings.couchbase_cdc_overlap_s * 1_000_000_000
        page = settings.couchbase_cdc_page_size
        watermark = {c: int((checkpoint.get(c) or {}).get("cas") or 0) for c in containers}
        seen = SeenRecently()

        def _query(container: str, since: int) -> list[dict[str, Any]]:
            scope, collection = split_container(container)
            statement = (
                f"SELECT META(d).id AS id, META(d).cas AS cas, d AS doc "
                f"FROM `{bucket}`.`{scope}`.`{collection}` AS d "
                f"WHERE META(d).cas >= $since ORDER BY META(d).cas LIMIT {page}"
            )
            result = cluster.query(
                statement,
                QueryOptions(named_parameters={"since": since}, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
            )
            return list(result.rows())

        while True:
            for container in containers:
                since = watermark[container]
                max_seen = since
                while True:
                    try:
                        rows = await asyncio.to_thread(_query, container, since)
                    except CouchbaseException as exc:
                        raise SourceConnectorError(
                            f"Change poll failed for '{container}' (does it have a primary or META().cas "
                            f"index?): {exc}"
                        ) from exc
                    fresh = 0
                    for row in rows:
                        key, cas = row["id"], int(row["cas"])
                        max_seen = max(max_seen, cas)
                        if not seen.add((key, cas)):
                            continue
                        fresh += 1
                        yield ChangeEvent(
                            container=container, op="upsert",
                            document=SourceDocument(key=key, body=row["doc"], container=container, source_key={"key": key}),
                            checkpoint={"cas": max(since - overlap_ns, 0)},
                            source_ts=_cas_to_datetime(cas),
                        )
                    if len(rows) < page or fresh == 0:
                        break
                    since = max_seen
                watermark[container] = max(max_seen - overlap_ns, watermark[container])
                yield ChangeEvent(container=container, op="checkpoint", checkpoint={"cas": watermark[container]})
            yield heartbeat()
            await asyncio.sleep(settings.couchbase_cdc_poll_interval_s)


    async def list_keys(self, container: str) -> AsyncIterator[str]:
        cluster = await asyncio.to_thread(self._connect)
        scope, collection = split_container(container)
        statement = f"SELECT RAW META().id FROM `{self._bucket()}`.`{scope}`.`{collection}`"
        rows = await asyncio.to_thread(lambda: list(cluster.query(statement).rows()))
        for key in rows:
            yield key


    async def apply_reverse(
        self, container: str, op: str, key: str,
        body: dict[str, Any] | None, source_key: dict[str, Any] | None, types: dict[str, str] | None = None,
    ) -> None:
        cluster = await asyncio.to_thread(self._connect)
        scope, collection = split_container(container)
        coll = cluster.bucket(self._bucket()).scope(scope).collection(collection)
        if op == "delete":
            try:
                await asyncio.to_thread(coll.remove, key)
            except DocumentNotFoundException:
                pass
            return
        await asyncio.to_thread(coll.upsert, key, body or {})


def _container_name(scope: str, collection: str) -> str:
    if scope == "_default":
        return collection
    return f"{scope}.{collection}"


def _looks_like_missing_index(exc: Exception) -> bool:
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in _NO_INDEX_MARKERS)


def split_container(container: str) -> tuple[str, str]:
    """Inverse of _container_name(): "scope.collection" or bare "collection"."""
    if "." in container:
        scope, collection = container.split(".", 1)
        return scope, collection
    return "_default", container


def _cas_to_datetime(cas: int) -> _dt.datetime | None:
    try:
        return _dt.datetime.utcfromtimestamp(cas / 1_000_000_000)
    except (OverflowError, OSError, ValueError):
        return None
