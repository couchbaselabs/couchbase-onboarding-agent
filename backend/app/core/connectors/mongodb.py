"""
MongoDB source connector -- the reference-depth implementation (see README/
ARCHITECTURE for why this one gets the deepest treatment: it establishes the
pattern the other four connectors follow).

- test_connection(): server version/edition, replica-set detection (Change Streams
  require a replica set or sharded cluster -- a standalone mongod can't produce
  them at all, which is surfaced here rather than failing confusingly later),
  per-collection document counts/sizes via collStats, and a sample of field names
  per collection for the wizard's schema preview.
- extract(): a full collection scan via find(), batched, with BSON types (ObjectId,
  datetime, Decimal128, Binary, DBRef) converted to JSON-safe values.
- capture_change_position(): opens a change stream per collection before the
  bulk copy and records its resume token (or the cluster's operationTime), so
  writes made during the copy are replayed afterwards.
- stream_changes(): MongoDB Change Streams, the native CDC mechanism -- resumable
  via a resume token (opaque, returned by the driver) persisted as this
  connector's checkpoint. Captures inserts, updates, replaces and deletes.
- apply_reverse(): post-cutover fallback writes (replace/delete by _id), with
  ObjectId/date/Decimal128/binary values restored from the recorded type hints.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import datetime as _dt
import decimal

from bson import Binary, ObjectId, Timestamp
from bson.decimal128 import Decimal128
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.connectors.base import (
    CdcCapabilities,
    ChangeEvent,
    SourceConnector,
    SourceConnectorError,
    SourceDocument,
    heartbeat,
)
from app.core.tls import ca_file_for
from app.core.connectors.util import (
    IDLE,
    apply_type_hints,
    bridge_blocking_batches,
    decode_binary,
    json_safe_typed,
    make_key,
    parse_iso_datetime,
)
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)


def _bson_hook(value: Any) -> tuple[Any, str] | None:
    if isinstance(value, ObjectId):
        return str(value), "oid"
    if isinstance(value, Decimal128):
        return float(value.to_decimal()), "decimal128"
    if isinstance(value, _dt.datetime):
        return value.isoformat(), "date"
    if isinstance(value, (bytes, Binary)):
        import base64

        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}, "binary"
    return None


def _transform_doc(raw: dict) -> tuple[dict, dict[str, str]]:
    types: dict[str, str] = {}
    body = json_safe_typed(raw, _bson_hook, types)
    return body, types


def _to_source_document(container: str, raw: dict) -> SourceDocument:
    raw_id = raw.get("_id")
    doc_id = str(raw_id)
    body, types = _transform_doc(raw)
    body["_id"] = doc_id
    return SourceDocument(
        key=make_key(container, doc_id), body=body, container=container,
        source_key={"_id": doc_id}, types=types or None,
    )


def _from_hint(value: Any, type_name: str) -> Any:
    if type_name == "oid":
        return ObjectId(value)
    if type_name == "date":
        return parse_iso_datetime(value)
    if type_name == "decimal128":
        return Decimal128(decimal.Decimal(str(value)))
    if type_name == "binary":
        return Binary(decode_binary(value))
    return value


def _checkpoint_kwargs(value: Any) -> dict[str, Any]:
    """Translate a stored checkpoint into watch() arguments. Two forms: a change
    stream resume token ({"_data": ...}) or an operation time recorded before a
    bulk copy ({"$ts": {"t": ..., "i": ...}})."""
    if not value:
        return {}
    if isinstance(value, dict) and "$ts" in value:
        ts = value["$ts"]
        return {"start_at_operation_time": Timestamp(int(ts["t"]), int(ts["i"]))}
    return {"resume_after": value}


class MongoDBConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._client: MongoClient | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(
            method=CdcMethod.CHANGE_STREAM, resumable=True, captures_deletes=True, reverse_sync=True,
        )

    def _connect(self) -> MongoClient:
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 15000}
        if self.config.username:
            kwargs["username"] = self.config.username
        if self.config.password:
            kwargs["password"] = self.config.password
        if self.config.use_tls:
            kwargs["tls"] = True
            ca_file = ca_file_for(self.config)
            if ca_file:
                kwargs["tlsCAFile"] = ca_file
            if self.config.tls_skip_verify:
                kwargs["tlsAllowInvalidCertificates"] = True
        try:
            client = MongoClient(self.config.connection_string, **kwargs)
            client.admin.command("ping")
        except PyMongoError as exc:
            raise SourceConnectorError(
                f"Could not connect to MongoDB ({self.config.label}): {exc}"
            ) from exc
        self._client = client
        return client

    def _db(self):
        client = self._connect()
        if self.config.database:
            return client[self.config.database]
        db = client.get_default_database()
        if db is None:
            raise SourceConnectorError(
                "No database specified. Set 'database' or include it in the connection string."
            )
        return db

    async def close(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> SourceTopologySnapshot:
        client = self._connect()
        db = self._db()

        build_info = client.admin.command("buildInfo")
        server_version = build_info.get("version", "unknown")

        is_replica_set = False
        try:
            hello = client.admin.command("hello")
            is_replica_set = bool(hello.get("setName")) or bool(hello.get("msg") == "isdbgrid")
        except PyMongoError:
            pass

        cdc_notes = None
        if not is_replica_set:
            cdc_notes = (
                "This MongoDB deployment does not appear to be a replica set or sharded "
                "cluster. Change Streams (required for continuous or hybrid replication) are "
                "unavailable on a standalone mongod -- convert it to a single-node replica set "
                "(`rs.initiate()`) to enable them, or use one-time migration instead."
            )

        containers: list[SourceContainerStats] = []
        total_count = 0
        total_size = 0
        for coll_name in db.list_collection_names():
            if coll_name.startswith("system."):
                continue
            notes: list[str] = []
            try:
                stats = db.command("collStats", coll_name)
                count = int(stats.get("count", 0) or 0)
                size = int(stats.get("size", 0) or 0)
            except PyMongoError:
                count = db[coll_name].estimated_document_count()
                size = 0
                notes.append("Could not read collStats (needs a role with collStats privilege); size is unknown.")

            sample_fields: list[str] = []
            try:
                sample = db[coll_name].find_one()
                if sample:
                    sample_fields = [k for k in sample.keys() if k != "_id"][:25]
            except PyMongoError:
                pass

            containers.append(SourceContainerStats(
                name=coll_name, estimated_count=count, estimated_size_bytes=size or None,
                sample_fields=sample_fields, notes=notes,
            ))
            total_count += count
            total_size += size

        return self.describe_capabilities(SourceTopologySnapshot(
            source_type=SourceType.MONGODB,
            server_version=server_version,
            server_edition=f"MongoDB {server_version} ({'replica set' if is_replica_set else 'standalone'})",
            containers=containers,
            total_estimated_count=total_count,
            total_estimated_size_bytes=total_size or None,
            supports_cdc=is_replica_set,
            cdc_notes=cdc_notes,
        ))

    # -- extraction -------------------------------------------------------------

    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        db = self._db()
        for container in containers:
            def _produce(q: queue.Queue, container=container) -> None:
                try:
                    coll = db[container]
                    # Atlas's free/shared (and some serverless) tiers reject
                    # no_cursor_timeout cursors outright (code 8000, AtlasError),
                    # failing on the very first find/getMore before any documents
                    # are returned. Prefer no_cursor_timeout (needed on self-managed
                    # deployments so a slow sink doesn't let the server reap the
                    # cursor mid-scan), but fall back transparently when Atlas
                    # refuses it -- nothing has been queued yet at that point, so
                    # retrying from scratch is safe.
                    no_cursor_timeout = True
                    while True:
                        batch: list[SourceDocument] = []
                        cursor = coll.find({}, no_cursor_timeout=no_cursor_timeout).batch_size(batch_size)
                        try:
                            for raw in cursor:
                                batch.append(_to_source_document(container, raw))
                                if len(batch) >= batch_size:
                                    q.put(batch)
                                    batch = []
                            if batch:
                                q.put(batch)
                            break
                        except PyMongoError as exc:
                            if no_cursor_timeout and "noTimeout cursors are disallowed" in str(exc):
                                logger.warning(
                                    "MongoDB source '%s' rejected no_cursor_timeout on collection "
                                    "'%s' (Atlas free/shared-tier restriction); retrying without it.",
                                    self.config.label, container,
                                )
                                no_cursor_timeout = False
                                continue
                            raise
                        finally:
                            cursor.close()
                except Exception as exc:  # noqa: BLE001
                    q.put(SourceConnectorError(f"MongoDB extraction failed for collection '{container}': {exc}"))
                finally:
                    q.put(None)

            async for batch in bridge_blocking_batches(_produce):
                if batch is IDLE:
                    continue
                await sink(batch)

    # -- change data capture -----------------------------------------------------

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._capture_sync, containers)

    def _capture_sync(self, containers: list[str]) -> dict[str, Any]:
        db = self._db()
        out: dict[str, Any] = {}
        op_time = None
        for container in containers:
            token = None
            try:
                with db[container].watch(full_document="updateLookup", max_await_time_ms=100) as stream:
                    stream.try_next()
                    token = stream.resume_token
            except PyMongoError as exc:
                raise SourceConnectorError(
                    f"Could not open a change stream on '{container}' (change streams need a replica "
                    f"set or sharded cluster): {exc}"
                ) from exc
            if token is None:
                if op_time is None:
                    op_time = self._connect().admin.command("ping").get("operationTime")
                if op_time is not None:
                    token = {"$ts": {"t": op_time.time, "i": op_time.inc}}
            if token is not None:
                out[container] = token
        return out

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        """Watches every container's Change Stream concurrently -- one background
        thread per container, all feeding a single shared queue -- since a change
        stream blocks waiting for the next event and containers must be watched in
        parallel. Errors surface to the engine, which restarts from the last
        persisted resume token."""
        db = self._db()
        q: queue.Queue = queue.Queue(maxsize=256)
        stop = threading.Event()

        def _watch_one(container: str) -> None:
            try:
                kwargs: dict[str, Any] = {"full_document": "updateLookup", "max_await_time_ms": 1000}
                kwargs.update(_checkpoint_kwargs(checkpoint.get(container)))
                with db[container].watch(**kwargs) as stream:
                    while not stop.is_set() and stream.alive:
                        change = stream.try_next()
                        if change is not None:
                            q.put((container, change))
            except Exception as exc:  # noqa: BLE001
                q.put((container, SourceConnectorError(
                    f"MongoDB change stream failed for collection '{container}': {exc}"
                )))

        threads = [threading.Thread(target=_watch_one, args=(c,), daemon=True) for c in containers]
        for t in threads:
            t.start()

        try:
            while True:
                try:
                    container, change = await asyncio.to_thread(q.get, True, 2.0)
                except queue.Empty:
                    yield heartbeat()
                    continue
                if isinstance(change, BaseException):
                    raise change

                op_type = change.get("operationType")
                token = change.get("_id")
                cluster_time = change.get("clusterTime")
                source_ts = cluster_time.as_datetime().replace(tzinfo=None) if cluster_time else None
                doc_key = change.get("documentKey", {})
                mongo_id = str(doc_key.get("_id")) if doc_key else None
                if op_type in ("insert", "update", "replace") and change.get("fullDocument") is not None:
                    yield ChangeEvent(
                        container=container, op="upsert",
                        document=_to_source_document(container, change["fullDocument"]),
                        checkpoint=token, source_ts=source_ts,
                    )
                elif op_type in ("insert", "update", "replace") and mongo_id:
                    # fullDocument is null when the document was deleted before
                    # the update lookup ran; a later delete event follows.
                    continue
                elif op_type == "delete" and mongo_id:
                    yield ChangeEvent(
                        container=container, op="delete", key=make_key(container, mongo_id),
                        checkpoint=token, source_ts=source_ts,
                    )
                elif op_type in ("drop", "rename", "dropDatabase", "invalidate"):
                    raise SourceConnectorError(
                        f"MongoDB collection '{container}' was {op_type}ped at the source; its change "
                        "stream ended. Re-run the bulk copy for it once it exists again."
                    )
        finally:
            stop.set()

    # -- reconciliation / fallback ----------------------------------------------

    async def list_keys(self, container: str) -> AsyncIterator[str]:
        db = self._db()

        def _produce(q: queue.Queue) -> None:
            try:
                batch: list[str] = []
                for raw in db[container].find({}, {"_id": 1}).batch_size(5000):
                    batch.append(make_key(container, str(raw.get("_id"))))
                    if len(batch) >= 5000:
                        q.put(batch)
                        batch = []
                if batch:
                    q.put(batch)
            except Exception as exc:  # noqa: BLE001
                q.put(SourceConnectorError(f"MongoDB key listing failed for '{container}': {exc}"))
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
        raw_id: Any = (source_key or {}).get("_id")
        if raw_id is None and body is not None:
            raw_id = body.get("_id")
        if raw_id is None:
            raw_id = key.split("::", 1)[1] if "::" in key else key
        id_value = _from_hint(raw_id, types["/_id"]) if types and types.get("/_id") else raw_id
        coll = self._db()[container]
        if op == "delete":
            await asyncio.to_thread(coll.delete_one, {"_id": id_value})
            return
        doc = apply_type_hints(dict(body or {}), types, _from_hint)
        doc["_id"] = id_value
        await asyncio.to_thread(coll.replace_one, {"_id": id_value}, doc, True)
