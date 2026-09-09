"""
Amazon DynamoDB source connector.

- test_connection(): lists tables, reads each table's (periodically-updated,
  approximate -- this is DynamoDB's own behavior, not a limitation of this app)
  ItemCount/TableSizeBytes, and checks whether DynamoDB Streams is enabled (the
  CDC prerequisite) plus its StreamViewType.
- extract(): a full Scan per table, paginated via ExclusiveStartKey, converting
  DynamoDB's AttributeValue JSON into plain JSON via boto3's TypeDeserializer.
- stream_changes(): DynamoDB Streams. LIGHTER-DEPTH IMPLEMENTATION (see README's
  connector-depth notes): reads each shard once from where the checkpoint (or
  TRIM_HORIZON) says to start and polls GetRecords in a loop, but does not follow
  shard splits/merges -- a shard that closes and is replaced by children shards
  (which DynamoDB does periodically, and always when a table's throughput
  characteristics change) will stop producing new events until the migration is
  restarted, at which point describe_stream() is called again and picks up the
  current shard set. Fine for a demo/reference-quality continuous sync; a
  production deployment migrating a high-throughput table would want the KCL
  (Kinesis Client Library) style shard-tree-following logic AWS's own DynamoDB
  Streams Kinesis Adapter provides.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from app.core.connectors.base import ChangeEvent, SourceConnector, SourceConnectorError, SourceDocument
from app.core.connectors.util import IDLE, bridge_blocking_batches, json_safe, make_key
from app.models.enums import SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)

_deserializer = TypeDeserializer()


def _deserialize_item(item: dict) -> dict:
    return {k: json_safe(_deserializer.deserialize(v)) for k, v in item.items()}


class DynamoDBConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._session: boto3.Session | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

    def _session_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.config.aws_region:
            kwargs["region_name"] = self.config.aws_region
        if self.config.aws_access_key_id:
            kwargs["aws_access_key_id"] = self.config.aws_access_key_id
        if self.config.aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self.config.aws_secret_access_key
        if self.config.aws_session_token:
            kwargs["aws_session_token"] = self.config.aws_session_token
        return kwargs

    def _client(self):
        kwargs: dict[str, Any] = {}
        if self.config.dynamodb_endpoint_url:
            kwargs["endpoint_url"] = self.config.dynamodb_endpoint_url
        if self._session is None:
            self._session = boto3.Session(**self._session_kwargs())
        return self._session.client("dynamodb", **kwargs)

    def _streams_client(self):
        kwargs: dict[str, Any] = {}
        if self.config.dynamodb_endpoint_url:
            kwargs["endpoint_url"] = self.config.dynamodb_endpoint_url
        if self._session is None:
            self._session = boto3.Session(**self._session_kwargs())
        return self._session.client("dynamodbstreams", **kwargs)

    async def close(self) -> None:
        self._session = None

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        return await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> SourceTopologySnapshot:
        client = self._client()
        try:
            table_names: list[str] = []
            start = None
            while True:
                kwargs = {"ExclusiveStartTableName": start} if start else {}
                resp = client.list_tables(**kwargs)
                table_names.extend(resp.get("TableNames", []))
                start = resp.get("LastEvaluatedTableName")
                if not start:
                    break
        except (BotoCoreError, ClientError) as exc:
            raise SourceConnectorError(f"Could not connect to DynamoDB ({self.config.label}): {exc}") from exc

        containers: list[SourceContainerStats] = []
        total_count = 0
        total_size = 0
        any_stream_enabled = False
        cdc_notes_parts: list[str] = []

        for name in table_names:
            desc = client.describe_table(TableName=name)["Table"]
            count = int(desc.get("ItemCount", 0) or 0)
            size = int(desc.get("TableSizeBytes", 0) or 0)
            stream_spec = desc.get("StreamSpecification", {})
            stream_enabled = bool(stream_spec.get("StreamEnabled"))
            notes = ["ItemCount/TableSizeBytes are DynamoDB's own periodically-updated estimates, "
                     "not a live count."]
            if not stream_enabled:
                notes.append("DynamoDB Streams is not enabled on this table -- required for continuous sync.")
            else:
                any_stream_enabled = True
                view_type = stream_spec.get("StreamViewType", "")
                if view_type not in ("NEW_IMAGE", "NEW_AND_OLD_IMAGES"):
                    notes.append(
                        f"Stream view type is {view_type or 'unknown'}; continuous sync needs NEW_IMAGE or "
                        "NEW_AND_OLD_IMAGES to capture full item content on insert/update."
                    )

            sample_fields: list[str] = []
            try:
                sample = client.scan(TableName=name, Limit=1).get("Items", [])
                if sample:
                    sample_fields = list(_deserialize_item(sample[0]).keys())[:25]
            except (BotoCoreError, ClientError):
                pass

            containers.append(SourceContainerStats(
                name=name, estimated_count=count, estimated_size_bytes=size or None,
                sample_fields=sample_fields, notes=notes,
            ))
            total_count += count
            total_size += size

        if not any_stream_enabled and table_names:
            cdc_notes_parts.append(
                "No included table has DynamoDB Streams enabled. Enable it (with NEW_IMAGE or "
                "NEW_AND_OLD_IMAGES view type) per table before selecting a continuous replication mode."
            )

        return SourceTopologySnapshot(
            source_type=SourceType.DYNAMODB,
            server_version=None,
            server_edition="Amazon DynamoDB",
            containers=containers,
            total_estimated_count=total_count,
            total_estimated_size_bytes=total_size or None,
            supports_cdc=any_stream_enabled,
            cdc_notes=" ".join(cdc_notes_parts) or None,
        )

    # -- extraction -------------------------------------------------------------

    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        client = self._client()
        for table in containers:
            key_attrs = self._key_attrs(client, table)

            def _produce(q: queue.Queue, table=table, key_attrs=key_attrs) -> None:
                try:
                    batch: list[SourceDocument] = []
                    kwargs: dict[str, Any] = {"TableName": table, "Limit": batch_size}
                    while True:
                        resp = client.scan(**kwargs)
                        for item in resp.get("Items", []):
                            body = _deserialize_item(item)
                            doc_key = _key_for(table, key_attrs, body)
                            batch.append(SourceDocument(key=doc_key, body=body, container=table))
                            if len(batch) >= batch_size:
                                q.put(batch)
                                batch = []
                        last_key = resp.get("LastEvaluatedKey")
                        if not last_key:
                            break
                        kwargs["ExclusiveStartKey"] = last_key
                    if batch:
                        q.put(batch)
                except (BotoCoreError, ClientError) as exc:
                    q.put(SourceConnectorError(f"DynamoDB scan failed for table '{table}': {exc}"))
                finally:
                    q.put(None)

            async for batch in bridge_blocking_batches(_produce):
                if batch is IDLE:
                    continue
                await sink(batch)

    @staticmethod
    def _key_attrs(client, table: str) -> list[str]:
        desc = client.describe_table(TableName=table)["Table"]
        return [k["AttributeName"] for k in desc.get("KeySchema", [])]

    # -- change data capture -----------------------------------------------------

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        client = self._client()
        streams = self._streams_client()
        q: queue.Queue = queue.Queue(maxsize=32)
        # Per-table accumulated {shard_id: sequence_number}, so a checkpoint
        # persisted for one shard's progress doesn't clobber the others' -- see
        # MigrationRecord.checkpoint, which MigrationEngine sets wholesale from
        # ChangeEvent.checkpoint on every event (per-container, not per-shard).
        table_checkpoints: dict[str, dict[str, str]] = {t: dict(checkpoint.get(t) or {}) for t in containers}
        table_key_attrs: dict[str, list[str]] = {}

        def _watch_table(table: str) -> None:
            try:
                desc = client.describe_table(TableName=table)["Table"]
                table_key_attrs[table] = [k["AttributeName"] for k in desc.get("KeySchema", [])]
                stream_arn = desc.get("LatestStreamArn")
                if not stream_arn:
                    q.put((table, SourceConnectorError(
                        f"DynamoDB Streams is not enabled on table '{table}'."
                    )))
                    return
                shard_ids = [
                    s["ShardId"] for s in streams.describe_stream(StreamArn=stream_arn)["StreamDescription"]["Shards"]
                ]
                table_checkpoint: dict[str, str] = (checkpoint.get(table) or {})
                shard_threads = [
                    threading.Thread(
                        target=_watch_shard, args=(table, stream_arn, shard_id, table_checkpoint.get(shard_id)),
                        daemon=True,
                    )
                    for shard_id in shard_ids
                ]
                for t in shard_threads:
                    t.start()
                for t in shard_threads:
                    t.join()
            except (BotoCoreError, ClientError) as exc:
                q.put((table, SourceConnectorError(f"DynamoDB Streams setup failed for table '{table}': {exc}")))

        def _watch_shard(table: str, stream_arn: str, shard_id: str, after_sequence: str | None) -> None:
            try:
                if after_sequence:
                    iterator_resp = streams.get_shard_iterator(
                        StreamArn=stream_arn, ShardId=shard_id,
                        ShardIteratorType="AFTER_SEQUENCE_NUMBER", SequenceNumber=after_sequence,
                    )
                else:
                    iterator_resp = streams.get_shard_iterator(
                        StreamArn=stream_arn, ShardId=shard_id, ShardIteratorType="TRIM_HORIZON",
                    )
                shard_iterator = iterator_resp.get("ShardIterator")
                while shard_iterator:
                    resp = streams.get_records(ShardIterator=shard_iterator, Limit=100)
                    for record in resp.get("Records", []):
                        q.put((table, (shard_id, record)))
                    shard_iterator = resp.get("NextShardIterator")
                    if not resp.get("Records"):
                        time.sleep(1.0)  # DynamoDB Streams recommends >=1s between empty polls
            except (BotoCoreError, ClientError) as exc:
                q.put((table, SourceConnectorError(
                    f"DynamoDB Streams read failed for table '{table}' shard '{shard_id}': {exc}"
                )))

        threads = [threading.Thread(target=_watch_table, args=(t,), daemon=True) for t in containers]
        for t in threads:
            t.start()

        while True:
            try:
                table, payload = await asyncio.to_thread(q.get, True, 2.0)
            except queue.Empty:
                yield ChangeEvent(container="*", op="heartbeat")
                continue
            if isinstance(payload, BaseException):
                raise payload

            shard_id, record = payload
            event_name = record.get("eventName")
            ddb = record.get("dynamodb", {})
            table_checkpoints.setdefault(table, {})[shard_id] = ddb.get("SequenceNumber")
            checkpoint_value = dict(table_checkpoints[table])
            key_attrs = table_key_attrs.get(table, [])

            if event_name in ("INSERT", "MODIFY") and ddb.get("NewImage"):
                body = _deserialize_item(ddb["NewImage"])
                doc_key = _key_for(table, key_attrs, body)
                yield ChangeEvent(
                    container=table, op="upsert",
                    document=SourceDocument(key=doc_key, body=body, container=table),
                    checkpoint=checkpoint_value,
                )
            elif event_name == "REMOVE" and ddb.get("Keys"):
                key_body = _deserialize_item(ddb["Keys"])
                doc_key = _key_for(table, key_attrs or list(key_body.keys()), key_body)
                yield ChangeEvent(container=table, op="delete", key=doc_key, checkpoint=checkpoint_value)


def _key_for(table: str, key_attrs: list[str], body: dict) -> str:
    """Deterministic Couchbase key from a DynamoDB item using the table's real
    KeySchema (partition key, plus sort key if present) -- known via one
    describe_table call per table (see DynamoDBConnector._key_attrs /
    stream_changes' table_key_attrs), not guessed per item."""
    if key_attrs:
        parts = [str(body.get(k, "")) for k in key_attrs]
    else:
        scalars = {k: v for k, v in body.items() if isinstance(v, (str, int, float, bool))}
        parts = [str(scalars[k]) for k in sorted(scalars.keys())[:2]] if scalars else [str(body)]
    return make_key(table, *parts)
