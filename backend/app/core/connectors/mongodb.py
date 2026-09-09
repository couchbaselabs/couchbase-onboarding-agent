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
- stream_changes(): MongoDB Change Streams, the native CDC mechanism -- resumable
  via a resume token (opaque, returned by the driver) persisted as this
  connector's checkpoint.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.connectors.base import ChangeEvent, SourceConnector, SourceConnectorError, SourceDocument
from app.core.connectors.util import IDLE, bridge_blocking_batches, json_safe, make_key
from app.models.enums import SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)


def _transform_value(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return json_safe(value)


def _transform_doc(raw: dict) -> dict:
    return {k: _transform_value(v) for k, v in raw.items()}


class MongoDBConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._client: MongoClient | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

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
            if self.config.ca_cert_path:
                kwargs["tlsCAFile"] = self.config.ca_cert_path
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

        return SourceTopologySnapshot(
            source_type=SourceType.MONGODB,
            server_version=server_version,
            server_edition=f"MongoDB {server_version} ({'replica set' if is_replica_set else 'standalone'})",
            containers=containers,
            total_estimated_count=total_count,
            total_estimated_size_bytes=total_size or None,
            supports_cdc=is_replica_set,
            cdc_notes=cdc_notes,
        )

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
                                doc_id = str(raw.get("_id"))
                                body = _transform_doc(raw)
                                body["_id"] = doc_id
                                batch.append(SourceDocument(
                                    key=make_key(container, doc_id), body=body, container=container,
                                ))
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

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        """Watches every container's Change Stream concurrently -- one background
        thread per container, all feeding a single shared queue -- rather than one
        at a time, since a change stream blocks indefinitely waiting for the next
        event and containers must be watched in parallel, not sequentially."""
        db = self._db()
        q: queue.Queue = queue.Queue(maxsize=32)

        def _watch_one(container: str) -> None:
            resume_token = checkpoint.get(container)
            try:
                coll = db[container]
                kwargs: dict[str, Any] = {"full_document": "updateLookup"}
                if resume_token:
                    kwargs["resume_after"] = resume_token
                with coll.watch(**kwargs) as stream:
                    for change in stream:
                        q.put((container, change))
            except Exception as exc:  # noqa: BLE001
                q.put((container, SourceConnectorError(
                    f"MongoDB change stream failed for collection '{container}': {exc}"
                )))

        threads = [
            threading.Thread(target=_watch_one, args=(c,), daemon=True) for c in containers
        ]
        for t in threads:
            t.start()

        while True:
            try:
                container, change = await asyncio.to_thread(q.get, True, 2.0)
            except queue.Empty:
                # Regular heartbeat so MigrationEngine can notice a stop request
                # even during a quiet period with no source changes on any container.
                yield ChangeEvent(container="*", op="heartbeat")
                continue
            if isinstance(change, BaseException):
                raise change

            op_type = change.get("operationType")
            token = change.get("_resume_token")
            doc_key = change.get("documentKey", {})
            mongo_id = str(doc_key.get("_id")) if doc_key else None
            if op_type in ("insert", "update", "replace") and change.get("fullDocument") is not None:
                body = _transform_doc(change["fullDocument"])
                body["_id"] = mongo_id
                yield ChangeEvent(
                    container=container, op="upsert",
                    document=SourceDocument(key=make_key(container, mongo_id), body=body, container=container),
                    checkpoint=token,
                )
            elif op_type == "delete" and mongo_id:
                yield ChangeEvent(
                    container=container, op="delete", key=make_key(container, mongo_id), checkpoint=token,
                )
            # "invalidate"/"drop"/other op types: nothing meaningful to replicate;
            # skip without advancing the checkpoint for this event.
