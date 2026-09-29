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
- stream_changes(): polling. Cassandra's own CDC (`cdc=true`) writes commit-log
  segments to each node's local disk, which a central service can't read, so
  this connector re-scans each table on an interval and emits rows whose newest
  WRITETIME() -- taken across every regular column, not just one -- is past the
  table's watermark. The watermark is the checkpoint, and it's set just before a
  bulk copy starts so writes during the copy are picked up.
    - Each poll streams the whole table (there's no server-side "changed since"
      filter), so CASSANDRA_CDC_POLL_INTERVAL_S is a cost/latency trade-off.
    - Deletes don't appear in a scan; the engine's periodic reconciliation
      (list_keys() against the destination) removes them.
    - A table whose only non-key columns are non-frozen collections or counters
      has nothing WRITETIME() can read; it's flagged during introspection.
- apply_reverse(): post-cutover fallback writes via INSERT ... JSON and
  DELETE by primary key.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import json
import logging
import queue
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from cassandra.auth import PlainTextAuthProvider
from cassandra.cluster import Cluster
from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy
from cassandra.query import dict_factory

from app.config import get_settings
from app.core.connectors.base import (
    CdcCapabilities,
    ChangeEvent,
    SourceConnector,
    SourceConnectorError,
    SourceDocument,
    heartbeat,
)
from app.core.connectors.util import (
    IDLE,
    bridge_blocking_batches,
    decode_binary,
    json_safe_typed,
    make_key,
)
from app.core.tls import ca_file_for
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)
settings = get_settings()


# Rows written up to this long before a bulk copy started are re-read by the
# first poll afterwards, to absorb clock skew between the service and the nodes.
_CAPTURE_MARGIN_US = 60 * 1_000_000


def _cql_hook(value: Any) -> tuple[Any, str] | None:
    if isinstance(value, _dt.datetime):
        return value.isoformat(), "timestamp"
    if isinstance(value, (bytes, bytearray)):
        import base64

        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}, "blob"
    return None


def _transform_row(row: dict, types: dict[str, str] | None = None) -> dict:
    hints: dict[str, str] = {} if types is None else types
    clean = {k: v for k, v in row.items() if not k.startswith("__cb_")}
    return json_safe_typed(clean, _cql_hook, hints)


def _row_document(table: str, key_cols: list[str], row: dict) -> SourceDocument:
    types: dict[str, str] = {}
    body = _transform_row(row, types)
    return SourceDocument(
        key=make_key(table, *[str(body.get(c)) for c in key_cols]), body=body, container=table,
        source_key={c: body.get(c) for c in key_cols}, types=types or None,
    )


def _json_for_insert(value: Any) -> Any:
    """Couchbase JSON -> what Cassandra's INSERT ... JSON accepts."""
    if isinstance(value, dict):
        if set(value.keys()) == {"$binary"}:
            return "0x" + decode_binary(value).hex()
        return {k: _json_for_insert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_for_insert(v) for v in value]
    if isinstance(value, str) and len(value) >= 19 and value[10:11] == "T" and value[4:5] == "-":
        # ISO timestamps: Cassandra wants at most millisecond precision.
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}" + (
                "Z" if parsed.tzinfo is None else parsed.strftime("%z")
            )
        except ValueError:
            return value
    return value


def _cql_value(cql_type: str, value: Any) -> Any:
    """Typed Python value for binding a primary-key column in a prepared DELETE."""
    t = cql_type.lower()
    if value is None:
        return None
    if t in ("uuid", "timeuuid"):
        return uuid.UUID(str(value))
    if t == "timestamp":
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t == "date":
        return _dt.date.fromisoformat(str(value))
    if t in ("int", "bigint", "smallint", "tinyint", "varint", "counter"):
        return int(value)
    if t in ("float", "double"):
        return float(value)
    if t == "decimal":
        return decimal.Decimal(str(value))
    if t == "boolean":
        return bool(value)
    if t == "blob":
        return decode_binary(value)
    return value


class CassandraConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._cluster: Cluster | None = None
        self._session = None

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(
            method=CdcMethod.WRITETIME_POLL, resumable=True, captures_deletes=False, reverse_sync=True,
        )

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
        ssl_context = None
        if self.config.use_tls:
            import ssl

            ssl_context = ssl.create_default_context(cafile=ca_file_for(self.config))
            if self.config.tls_skip_verify:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        try:
            self._cluster = Cluster(
                contact_points=contact_points, port=self.config.cassandra_port,
                auth_provider=auth_provider,
                load_balancing_policy=load_balancing,
                protocol_version=None,
                ssl_context=ssl_context,
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

            writetime_cols = _writetime_columns(table_meta, partition_cols, clustering_cols)
            if not writetime_cols:
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

        return self.describe_capabilities(SourceTopologySnapshot(
            source_type=SourceType.CASSANDRA,
            server_version=server_version,
            server_edition=f"Apache Cassandra {server_version or ''} (keyspace {keyspace})".strip(),
            containers=containers,
            total_estimated_count=total_count or None,
            total_estimated_size_bytes=None,
            supports_cdc=True,
            cdc_notes=(
                "Continuous sync polls each table on an interval using WRITETIME() rather than "
                "Cassandra's commit-log CDC (which needs filesystem access on every node). Deletes "
                "reach Couchbase through the periodic reconciliation instead of the change stream."
            ),
        ))

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
                        batch.append(_row_document(table, key_cols, row))
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

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        now_us = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1_000_000)
        return {c: now_us - _CAPTURE_MARGIN_US for c in containers}

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        session = self._connect()
        keyspace = self._keyspace()
        ks_meta = self._cluster.metadata.keyspaces[keyspace]  # type: ignore[union-attr]

        watermark: dict[str, int] = {c: int(checkpoint.get(c) or 0) for c in containers}
        poll_interval = settings.cassandra_cdc_poll_interval_s

        while True:
            for table in containers:
                table_meta = ks_meta.tables[table]
                partition_cols = {c.name for c in table_meta.partition_key}
                clustering_cols = {c.name for c in table_meta.clustering_key}
                wt_cols = _writetime_columns(table_meta, partition_cols, clustering_cols)
                if not wt_cols:
                    continue
                key_cols = _primary_key_columns(table_meta)
                since = watermark[table]
                max_seen = since

                def _produce(q: queue.Queue, table=table, wt_cols=wt_cols) -> None:
                    try:
                        wt_select = ", ".join(f'WRITETIME("{c}") AS "__cb_wt{i}"' for i, c in enumerate(wt_cols))
                        stmt = session.prepare(f'SELECT *, {wt_select} FROM "{table}"')
                        stmt.fetch_size = 2000
                        batch: list[tuple[dict, int]] = []
                        for row in session.execute(stmt):
                            stamps = [row.get(f"__cb_wt{i}") for i in range(len(wt_cols))]
                            wt = max((s for s in stamps if s is not None), default=None)
                            if wt is not None and wt > since:
                                batch.append((row, wt))
                                if len(batch) >= 500:
                                    q.put(batch)
                                    batch = []
                        if batch:
                            q.put(batch)
                    except Exception as exc:  # noqa: BLE001
                        q.put(SourceConnectorError(f"Cassandra change poll failed for table '{table}': {exc}"))
                    finally:
                        q.put(None)

                async for rows in bridge_blocking_batches(_produce):
                    if rows is IDLE:
                        # Mid-scan: still reading, not caught up.
                        yield ChangeEvent(container="*", op="heartbeat", extra={"busy": True})
                        continue
                    for row, wt in rows:
                        max_seen = max(max_seen, wt)
                        yield ChangeEvent(
                            container=table, op="upsert", document=_row_document(table, key_cols, row),
                            # The watermark only advances once the whole table has
                            # been scanned (below); mid-scan events carry the old one
                            # so a restart re-scans rather than skipping rows.
                            checkpoint=since,
                            source_ts=_dt.datetime.utcfromtimestamp(wt / 1_000_000),
                        )
                watermark[table] = max_seen
                yield ChangeEvent(container=table, op="checkpoint", checkpoint=max_seen)

            yield heartbeat()
            await asyncio.sleep(poll_interval)

    # -- reconciliation / fallback ----------------------------------------------

    async def list_keys(self, container: str) -> AsyncIterator[str]:
        session = self._connect()
        table_meta = self._cluster.metadata.keyspaces[self._keyspace()].tables[container]  # type: ignore[union-attr]
        key_cols = _primary_key_columns(table_meta)

        def _produce(q: queue.Queue) -> None:
            try:
                cols = ", ".join(f'"{c}"' for c in key_cols)
                stmt = session.prepare(f'SELECT {cols} FROM "{container}"')
                stmt.fetch_size = 5000
                batch: list[str] = []
                for row in session.execute(stmt):
                    body = _transform_row(row)
                    batch.append(make_key(container, *[str(body.get(c)) for c in key_cols]))
                    if len(batch) >= 5000:
                        q.put(batch)
                        batch = []
                if batch:
                    q.put(batch)
            except Exception as exc:  # noqa: BLE001
                q.put(SourceConnectorError(f"Cassandra key listing failed for '{container}': {exc}"))
            finally:
                q.put(None)

        async for batch in bridge_blocking_batches(_produce):
            if batch is IDLE:
                continue
            for key in batch:
                yield key

    async def apply_reverse(
        self,
        container: str,
        op: str,
        key: str,
        body: dict[str, Any] | None,
        source_key: dict[str, Any] | None,
        types: dict[str, str] | None = None,
    ) -> None:
        session = self._connect()
        table_meta = self._cluster.metadata.keyspaces[self._keyspace()].tables[container]  # type: ignore[union-attr]
        key_cols = _primary_key_columns(table_meta)
        if op == "delete":
            ident = dict(source_key or {})
            missing = [c for c in key_cols if c not in ident]
            if missing:
                raise SourceConnectorError(
                    f"Can't delete '{key}' from Cassandra table '{container}': primary key columns "
                    f"{missing} are unknown."
                )
            where = " AND ".join(f'"{c}" = ?' for c in key_cols)
            stmt = session.prepare(f'DELETE FROM "{container}" WHERE {where}')
            values = [_cql_value(str(table_meta.columns[c].cql_type), ident[c]) for c in key_cols]
            await asyncio.to_thread(session.execute, stmt, values)
            return
        doc = {k: v for k, v in (body or {}).items() if k in table_meta.columns}
        payload = json.dumps(_json_for_insert(doc))
        await asyncio.to_thread(session.execute, f'INSERT INTO "{container}" JSON %s', (payload,))


def _primary_key_columns(table_meta) -> list[str]:
    return [c.name for c in table_meta.partition_key] + [c.name for c in table_meta.clustering_key]


def _writetime_columns(table_meta, partition_cols: set[str], clustering_cols: set[str]) -> list[str]:
    """Every regular column WRITETIME() can read: not part of the primary key,
    not a counter, not a non-frozen collection (frozen ones are single cells)."""
    out = []
    for name, col in table_meta.columns.items():
        if name in partition_cols or name in clustering_cols:
            continue
        cql_type = str(getattr(col, "cql_type", "") or "")
        if cql_type == "counter" or cql_type.startswith(("list<", "set<", "map<")):
            continue
        out.append(name)
    return out
