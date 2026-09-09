"""
Apache Cassandra source connector.

- test_connection(): connects to the configured keyspace, enumerates tables from
  driver-side cluster metadata, and estimates row counts from
  `system.size_estimates` (Cassandra's own approximate-partition-count table --
  the closest thing Cassandra has to MongoDB's collStats; it's per-node and
  approximate by design, which is noted on each container).
- extract(): a paged `SELECT *` per table using the driver's built-in paging
  (fetch_size), converting Cassandra-native types (uuid, decimal, date/time,
  blob, frozen collections) to JSON-safe values.
- stream_changes(): LIGHTER-DEPTH IMPLEMENTATION (see README's connector-depth
  notes). Cassandra's real CDC mechanism (`cdc=true` on a table) writes raw
  commit-log segments to a `cdc_raw` directory on each node's local filesystem --
  consuming that requires an agent co-located with every node, which this
  centrally-running app is not. Instead, this connector polls: on an interval, it
  re-scans each table and uses Cassandra's `WRITETIME(<column>)` function to find
  rows written since the last poll, using the per-table maximum writetime seen as
  the checkpoint. Trade-offs this implies, and why they're acceptable for this
  project's stated scope:
    - Every poll cycle re-scans the WHOLE table (Cassandra has no server-side
      "changed since" filter), so the poll interval is a real cost/latency
      trade-off, not just a knob -- see CASSANDRA_CDC_POLL_INTERVAL_S.
    - Deletes are NOT detected at all -- a row that's gone is invisible to a scan,
      there's no tombstone-diff logic here. A hard delete on the source will not
      be reflected in Couchbase via continuous sync; re-running a full load is the
      way to reconcile deletes.
    - Needs at least one non-primary-key, non-collection, non-counter column to
      call WRITETIME() on; a table with none of those (all-key or all-collection)
      can't be polled this way and is flagged during introspection instead of
      silently missing updates.
"""
from __future__ import annotations

import asyncio
import logging
import queue
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from cassandra.query import dict_factory

from app.config import get_settings
from app.core.connectors.base import ChangeEvent, SourceConnector, SourceConnectorError, SourceDocument
from app.core.connectors.util import IDLE, bridge_blocking_batches, json_safe, make_key
from app.models.enums import SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)
settings = get_settings()


def _transform_row(row: dict) -> dict:
    return {k: json_safe(v) for k, v in row.items() if not k.startswith("__cb_")}


class CassandraConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._cluster: Cluster | None = None
        self._session = None

    @property
    def supports_cdc(self) -> bool:
        return True

    def _connect(self):
        if self._session is not None:
            return self._session
        if not self.config.connection_string:
            raise SourceConnectorError("Cassandra requires at least one contact point.")
        contact_points = [h.split(":")[0].strip() for h in self.config.connection_string.split(",") if h.strip()]
        auth_provider = None
        if self.config.username:
            auth_provider = PlainTextAuthProvider(username=self.config.username, password=self.config.password or "")
        load_balancing = TokenAwarePolicy(DCAwareRoundRobinPolicy(
            local_dc=self.config.cassandra_datacenter
        )) if self.config.cassandra_datacenter else None
        try:
            self._cluster = Cluster(
                contact_points=contact_points, port=self.config.cassandra_port,
                auth_provider=auth_provider,
                load_balancing_policy=load_balancing,
                protocol_version=None,
            )
            self._session = self._cluster.connect(self.config.database) if self.config.database else self._cluster.connect()
            self._session.row_factory = dict_factory
        except Exception as exc:  # noqa: BLE001
            raise SourceConnectorError(f"Could not connect to Cassandra ({self.config.label}): {exc}") from exc
        return self._session

    async def close(self) -> None:
        if self._cluster is not None:
            await asyncio.to_thread(self._cluster.shutdown)
            self._cluster = None
            self._session = None

    def _keyspace(self) -> str:
        if not self.config.database:
            raise SourceConnectorError("Cassandra requires a keyspace (set 'database').")
        return self.config.database

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> SourceTopologySnapshot:
        session = self._connect()
        keyspace = self._keyspace()
        cluster_meta = self._cluster.metadata  # type: ignore[union-attr]
        server_version = None
        try:
            host = next(iter(self._cluster.metadata.all_hosts()))  # type: ignore[union-attr]
            server_version = str(getattr(host, "release_version", None) or "unknown")
        except StopIteration:
            pass

        ks_meta = cluster_meta.keyspaces.get(keyspace)
        if ks_meta is None:
            raise SourceConnectorError(f"Keyspace '{keyspace}' not found.")

        containers: list[SourceContainerStats] = []
        total_count = 0
        for table_name, table_meta in ks_meta.tables.items():
            partition_cols = {c.name for c in table_meta.partition_key}
            clustering_cols = {c.name for c in table_meta.clustering_key}
            all_cols = list(table_meta.columns.keys())
            notes: list[str] = []

            writetime_col = _pick_writetime_column(table_meta, partition_cols, clustering_cols)
            if writetime_col is None:
                notes.append(
                    "No regular (non-key, non-collection, non-counter) column available -- "
                    "continuous sync cannot poll for changes on this table."
                )

            estimate = 0
            try:
                rows = session.execute(
                    "SELECT partitions_count FROM system.size_estimates WHERE keyspace_name=%s AND table_name=%s",
                    (keyspace, table_name),
                )
                estimate = sum(r.get("partitions_count", 0) or 0 for r in rows)
                if estimate == 0:
                    notes.append(
                        "system.size_estimates has no data for this table yet (common on a freshly "
                        "written or very small table) -- count is unknown, not necessarily zero."
                    )
                else:
                    notes.append(
                        "Estimated partition count from system.size_estimates, per-node and "
                        "approximate -- not an exact row count."
                    )
            except Exception:  # noqa: BLE001
                notes.append("Could not read system.size_estimates; count is unknown.")

            containers.append(SourceContainerStats(
                name=table_name, estimated_count=estimate or None, estimated_size_bytes=None,
                sample_fields=[c for c in all_cols if c not in partition_cols][:25], notes=notes,
            ))
            total_count += estimate

        return SourceTopologySnapshot(
            source_type=SourceType.CASSANDRA,
            server_version=server_version,
            server_edition=f"Apache Cassandra {server_version or ''} (keyspace {keyspace})".strip(),
            containers=containers,
            total_estimated_count=total_count or None,
            total_estimated_size_bytes=None,
            supports_cdc=True,
            cdc_notes=(
                "Continuous sync polls each table on an interval using WRITETIME() rather than "
                "consuming Cassandra's commit-log CDC feature (which requires filesystem access on "
                "every node). Deletes are not detected this way -- see this connector's module "
                "docstring."
            ),
        )

    # -- extraction -------------------------------------------------------------

    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        session = self._connect()
        keyspace = self._keyspace()
        for table in containers:
            key_cols = _primary_key_columns(self._cluster.metadata.keyspaces[keyspace].tables[table])  # type: ignore[union-attr]

            def _produce(q: queue.Queue, table=table, key_cols=key_cols) -> None:
                try:
                    session.default_fetch_size = batch_size
                    result = session.execute(f'SELECT * FROM "{table}"')
                    batch: list[SourceDocument] = []
                    for row in result:
                        body = _transform_row(row)
                        key_parts = [str(body.get(c)) for c in key_cols]
                        batch.append(SourceDocument(
                            key=make_key(table, *key_parts), body=body, container=table,
                        ))
                        if len(batch) >= batch_size:
                            q.put(batch)
                            batch = []
                    if batch:
                        q.put(batch)
                except Exception as exc:  # noqa: BLE001
                    q.put(SourceConnectorError(f"Cassandra extraction failed for table '{table}': {exc}"))
                finally:
                    q.put(None)

            async for batch in bridge_blocking_batches(_produce):
                if batch is IDLE:
                    continue
                await sink(batch)

    # -- change data capture (polling) -------------------------------------------

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        session = self._connect()
        keyspace = self._keyspace()
        ks_meta = self._cluster.metadata.keyspaces[keyspace]  # type: ignore[union-attr]

        last_seen_us: dict[str, int] = {c: int(checkpoint.get(c) or 0) for c in containers}
        poll_interval = settings.cassandra_cdc_poll_interval_s

        while True:
            any_yielded = False
            for table in containers:
                table_meta = ks_meta.tables[table]
                partition_cols = {c.name for c in table_meta.partition_key}
                clustering_cols = {c.name for c in table_meta.clustering_key}
                writetime_col = _pick_writetime_column(table_meta, partition_cols, clustering_cols)
                if writetime_col is None:
                    continue
                key_cols = _primary_key_columns(table_meta)

                rows = await asyncio.to_thread(
                    self._scan_with_writetime, session, table, writetime_col,
                )
                max_seen = last_seen_us[table]
                for row, wt in rows:
                    if wt is None or wt <= last_seen_us[table]:
                        continue
                    any_yielded = True
                    max_seen = max(max_seen, wt)
                    body = _transform_row(row)
                    key_parts = [str(body.get(c)) for c in key_cols]
                    yield ChangeEvent(
                        container=table, op="upsert",
                        document=SourceDocument(key=make_key(table, *key_parts), body=body, container=table),
                        checkpoint=max_seen,
                    )
                last_seen_us[table] = max_seen

            if not any_yielded:
                yield ChangeEvent(container="*", op="heartbeat")
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _scan_with_writetime(session, table: str, writetime_col: str) -> list[tuple[dict, int | None]]:
        query = f'SELECT *, WRITETIME("{writetime_col}") AS __cb_writetime FROM "{table}"'
        session.default_fetch_size = 2000
        out = []
        for row in session.execute(query):
            wt = row.get("__cb_writetime")
            out.append((row, wt))
        return out


def _primary_key_columns(table_meta) -> list[str]:
    return [c.name for c in table_meta.partition_key] + [c.name for c in table_meta.clustering_key]


def _pick_writetime_column(table_meta, partition_cols: set[str], clustering_cols: set[str]) -> str | None:
    for name, col in table_meta.columns.items():
        if name in partition_cols or name in clustering_cols:
            continue
        cql_type = str(getattr(col, "cql_type", "") or "")
        if cql_type.startswith(("list<", "set<", "map<", "frozen<")) or cql_type == "counter":
            continue
        return name
    return None
