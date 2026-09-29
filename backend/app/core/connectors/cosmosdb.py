"""
Microsoft Azure Cosmos DB source connector (Core/SQL API).

Of the four "lighter-depth" connectors, this is the one closest to reference
depth: Cosmos DB's change feed is a native, durable, resumable (continuation-token
based) mechanism built into every container by default -- no special table/stream
configuration is required the way DynamoDB Streams or Redis keyspace notifications
need, so continuous sync here doesn't carry the same caveats those two do.

- test_connection(): reads each container's partition key path and an
  approximate document count (`SELECT VALUE COUNT(1) FROM c`, a cross-partition
  aggregate query Cosmos executes efficiently).
- extract(): `SELECT * FROM c` with cross-partition query enabled, paged by the
  SDK.
- capture_change_position(): records the time before a bulk copy starts; the
  change feed then starts from that time (not from the beginning of the
  container), so the copy isn't repeated and nothing written during it is lost.
- stream_changes(): the change feed in "Latest Version" mode (the default for
  every container), resumable by continuation token. This mode doesn't surface
  deletes; the engine's periodic reconciliation (list_keys() against the
  destination) removes them instead.
- apply_reverse(): post-cutover fallback writes (upsert/delete by id + partition key).

Uses azure-cosmos's native asyncio client throughout, same rationale as the Redis
connector -- no thread bridging needed.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from azure.core.exceptions import AzureError
from azure.cosmos.aio import CosmosClient

from app.config import get_settings
from app.core.connectors.base import (
    CdcCapabilities,
    ChangeEvent,
    SourceConnector,
    SourceConnectorError,
    SourceDocument,
    heartbeat,
)
from app.core.connectors.util import json_safe, make_key
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)
settings = get_settings()

_COSMOS_SYSTEM_PROPS = {"_rid", "_self", "_etag", "_attachments", "_ts"}


def _transform_item(item: dict) -> dict:
    body = {k: json_safe(v) for k, v in item.items() if k not in _COSMOS_SYSTEM_PROPS}
    ts = item.get("_ts")
    if ts is not None:
        body["_cosmos_ts"] = ts
    return body


def _pk_field_name(pk_path: str) -> str:
    # Only handles a single-level partition key path ("/customerId"), which covers
    # the overwhelming majority of Cosmos DB containers. Hierarchical partition
    # keys (multi-level paths, a newer Cosmos DB feature) fall back to the full
    # path string as the field name, which will not match any real document field
    # -- extract()/stream_changes() then key on `id` alone (see _doc_key), which
    # remains correct as long as `id` is unique within the container even without
    # the partition key folded in.
    return pk_path.lstrip("/").split("/")[0] if pk_path else ""


def _doc_key(container: str, item: dict, pk_field: str) -> str:
    pk_value = item.get(pk_field) if pk_field else None
    doc_id = str(item.get("id", ""))
    return make_key(container, str(pk_value), doc_id) if pk_value is not None else make_key(container, doc_id)


def _item_document(container: str, item: dict, pk_field: str) -> SourceDocument:
    source_key: dict[str, Any] = {"id": item.get("id")}
    if pk_field:
        source_key["pk"] = item.get(pk_field)
    return SourceDocument(
        key=_doc_key(container, item, pk_field), body=_transform_item(item), container=container,
        source_key=source_key,
    )


class CosmosDBConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._client: CosmosClient | None = None
        self._pk_field_cache: dict[str, str] = {}

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(
            method=CdcMethod.CHANGE_FEED, resumable=True, captures_deletes=False, reverse_sync=True,
        )

    def _connect(self) -> CosmosClient:
        if self._client is not None:
            return self._client
        if not self.config.cosmos_endpoint or not self.config.cosmos_key:
            raise SourceConnectorError("Cosmos DB requires 'cosmos_endpoint' and 'cosmos_key'.")
        self._client = CosmosClient(self.config.cosmos_endpoint, credential=self.config.cosmos_key)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _database_name(self) -> str:
        if not self.config.database:
            raise SourceConnectorError("Cosmos DB requires a database name (set 'database').")
        return self.config.database

    async def _pk_field(self, container) -> str:
        cache_key = container.container_link if hasattr(container, "container_link") else container.id
        if cache_key in self._pk_field_cache:
            return self._pk_field_cache[cache_key]
        props = await container.read()
        pk_path = (props.get("partitionKey", {}).get("paths") or [""])[0]
        field = _pk_field_name(pk_path)
        self._pk_field_cache[cache_key] = field
        return field

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        client = self._connect()
        try:
            db = client.get_database_client(self._database_name())
            await db.read()
        except AzureError as exc:
            raise SourceConnectorError(f"Could not connect to Cosmos DB ({self.config.label}): {exc}") from exc

        containers: list[SourceContainerStats] = []
        total_count = 0
        async for props in db.list_containers():
            name = props["id"]
            container = db.get_container_client(name)
            notes: list[str] = []
            count = None
            try:
                result = container.query_items(
                    query="SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True,
                )
                async for c in result:
                    count = int(c)
                    break
            except AzureError as exc:
                notes.append(f"Could not compute an item count: {exc}")

            sample_fields: list[str] = []
            try:
                sample_result = container.query_items(
                    query="SELECT TOP 1 * FROM c", enable_cross_partition_query=True,
                )
                async for item in sample_result:
                    sample_fields = [k for k in item.keys() if k not in _COSMOS_SYSTEM_PROPS][:25]
                    break
            except AzureError:
                pass

            containers.append(SourceContainerStats(
                name=name, estimated_count=count, estimated_size_bytes=None,
                sample_fields=sample_fields, notes=notes,
            ))
            total_count += count or 0

        return self.describe_capabilities(SourceTopologySnapshot(
            source_type=SourceType.COSMOSDB,
            server_version=None,
            server_edition="Azure Cosmos DB (Core/SQL API)",
            containers=containers,
            total_estimated_count=total_count or None,
            total_estimated_size_bytes=None,
            supports_cdc=True,
            cdc_notes=(
                "Uses the Cosmos DB change feed ('Latest Version' mode): durable and resumable. "
                "That mode doesn't report deletes, so deletes reach Couchbase through the periodic "
                "reconciliation instead of the change stream."
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
        client = self._connect()
        db = client.get_database_client(self._database_name())
        for name in containers:
            container = db.get_container_client(name)
            pk_field = await self._pk_field(container)
            batch: list[SourceDocument] = []
            async for item in container.query_items(query="SELECT * FROM c", enable_cross_partition_query=True):
                batch.append(_item_document(name, item, pk_field))
                if len(batch) >= batch_size:
                    await sink(batch)
                    batch = []
            if batch:
                await sink(batch)

    # -- change data capture -----------------------------------------------------

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        # A few seconds early to absorb clock skew; replaying is idempotent.
        start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)
        return {c: {"start_time": start.isoformat()} for c in containers}

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        client = self._connect()
        db = client.get_database_client(self._database_name())
        positions: dict[str, Any] = {c: checkpoint.get(c) for c in containers}

        while True:
            any_yielded = False
            for name in containers:
                container = db.get_container_client(name)
                pk_field = await self._pk_field(container)
                pos = positions.get(name)
                kwargs: dict[str, Any] = {}
                if isinstance(pos, dict) and pos.get("start_time"):
                    kwargs["start_time"] = _dt.datetime.fromisoformat(pos["start_time"])
                elif isinstance(pos, str):
                    kwargs["continuation"] = pos
                else:
                    kwargs["start_time"] = "Beginning"
                pages = container.query_items_change_feed(**kwargs).by_page()
                async for page in pages:
                    batch = [item async for item in page]
                    token = pages.continuation_token
                    for i, item in enumerate(batch):
                        any_yielded = True
                        ts = item.get("_ts")
                        yield ChangeEvent(
                            container=name, op="upsert", document=_item_document(name, item, pk_field),
                            # Only the last item of a page carries the page's token.
                            checkpoint=token if i == len(batch) - 1 else pos,
                            source_ts=_dt.datetime.utcfromtimestamp(ts) if ts else None,
                        )
                    if token:
                        positions[name] = token
                        pos = token

            if not any_yielded:
                yield heartbeat()
            await asyncio.sleep(settings.cosmosdb_change_feed_poll_interval_s)

    # -- reconciliation / fallback ----------------------------------------------

    async def list_keys(self, container_name: str) -> AsyncIterator[str]:
        client = self._connect()
        container = client.get_database_client(self._database_name()).get_container_client(container_name)
        pk_field = await self._pk_field(container)
        projection = "c.id" + (f", c[\"{pk_field}\"] AS __pk" if pk_field else "")
        async for item in container.query_items(
            query=f"SELECT {projection} FROM c", enable_cross_partition_query=True,
        ):
            if pk_field and "__pk" in item:
                item = {"id": item.get("id"), pk_field: item.get("__pk")}
            yield _doc_key(container_name, item, pk_field)

    async def apply_reverse(
        self,
        container: str,
        op: str,
        key: str,
        body: dict[str, Any] | None,
        source_key: dict[str, Any] | None,
        types: dict[str, str] | None = None,
    ) -> None:
        client = self._connect()
        cont = client.get_database_client(self._database_name()).get_container_client(container)
        pk_field = await self._pk_field(cont)
        if op == "delete":
            ident = dict(source_key or {})
            if "id" not in ident:
                raise SourceConnectorError(f"Can't delete '{key}' from Cosmos DB '{container}': its id is unknown.")
            await cont.delete_item(item=str(ident["id"]), partition_key=ident.get("pk"))
            return
        doc = {k: v for k, v in (body or {}).items() if k != "_cosmos_ts"}
        if "id" not in doc and source_key and source_key.get("id") is not None:
            doc["id"] = source_key["id"]
        if pk_field and pk_field not in doc and source_key and "pk" in source_key:
            doc[pk_field] = source_key["pk"]
        await cont.upsert_item(doc)
