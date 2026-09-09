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
  - Reports supports_cdc=True only for a self-managed Enterprise Edition
    source: XDCR (what continuous replication actually uses -- see
    couchbase_native.py) isn't available on Community Edition at all, and
    isn't implemented here for a Capella source (a real gap, not a silent
    assumption -- see couchbase_native.py's docstring, point 3).

extract() is intentionally NOT a real implementation -- see above -- and
raises immediately if ever called, rather than silently being dead code that
looks like it works.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import requests
from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import CouchbaseException
from couchbase.options import ClusterOptions

from app.core.connectors.base import SourceConnector, SourceConnectorError, SourceDocument
from app.models.enums import SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)

_NO_INDEX_MARKERS = ("No index available", "primary index", "index_not_found")


class CouchbaseSourceConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._cluster: Cluster | None = None

    @property
    def supports_cdc(self) -> bool:
        return self.config.source_type == SourceType.COUCHBASE_ENTERPRISE

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
            auth = PasswordAuthenticator(self.config.username or "", self.config.password or "")
            opts = ClusterOptions(auth, network="external" if self.config.couchbase_external_network else "auto")
            opts.apply_profile("wan_development")
            cluster = Cluster(self.config.connection_string, opts)
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
                verify=self.config.ca_cert_path if self.config.ca_cert_path else False, timeout=15,
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
        return await asyncio.to_thread(self._test_connection_sync)

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

        if self.supports_cdc:
            cdc_notes = (
                "Continuous replication uses Couchbase's built-in XDCR, configured directly on the "
                "source cluster (not this app's generic per-document CDC mechanism -- see "
                "core/couchbase_native.py)."
            )
        elif self._is_capella:
            cdc_notes = (
                "Continuous replication from a Capella source isn't implemented: it would need XDCR "
                "configured through Capella's own Management API rather than the classic REST API this "
                "app uses for self-managed clusters. One-time migration only for now."
            )
        else:
            cdc_notes = (
                "Continuous replication isn't available on Community Edition: it requires XDCR, which "
                "is an Enterprise Edition / Capella feature. One-time migration only."
            )

        return SourceTopologySnapshot(
            source_type=self.config.source_type,
            server_version=server_version,
            server_edition=f"Couchbase {(server_version + ' ') if server_version else ''}({edition})",
            containers=containers,
            total_estimated_count=total_count or None,
            total_estimated_size_bytes=None,
            supports_cdc=self.supports_cdc,
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


def _container_name(scope: str, collection: str) -> str:
    if scope == "_default":
        return collection
    return f"{scope}.{collection}"


def _looks_like_missing_index(exc: Exception) -> bool:
    text = str(exc)
    return any(marker.lower() in text.lower() for marker in _NO_INDEX_MARKERS)
