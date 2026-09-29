"""
Amazon DynamoDB source connector.

- test_connection(): lists tables, reads each table's (periodically-updated,
  approximate -- this is DynamoDB's own behavior, not a limitation of this app)
  ItemCount/TableSizeBytes, and checks whether DynamoDB Streams is enabled (the
  CDC prerequisite) plus its StreamViewType.
- extract(): a full Scan per table, paginated via ExclusiveStartKey, converting
  DynamoDB's AttributeValue JSON into plain JSON via boto3's TypeDeserializer.
- capture_change_position(): records the wall-clock start of the bulk copy.
  Shards without a sequence checkpoint are then read from TRIM_HORIZON, skipping
  records older than that moment (minus a safety margin), so nothing written
  during the copy is lost and 24 hours of history isn't replayed.
- stream_changes(): DynamoDB Streams with shard-lineage following: the shard set
  is re-discovered periodically, and a child shard is only read once its parent
  is exhausted (or has aged out), which preserves per-item ordering across
  splits -- the same rule the Kinesis Client Library applies.
- apply_reverse(): post-cutover fallback writes (PutItem/DeleteItem), restoring
  sets, binary and exact numbers from the recorded type hints.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import decimal
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import boto3
from boto3.dynamodb.types import Binary as DynamoBinary
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

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
    apply_type_hints,
    bridge_blocking_batches,
    decode_binary,
    json_safe_typed,
    make_key,
)
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)

_deserializer = TypeDeserializer()
_serializer = TypeSerializer()
settings = get_settings()

# Stream records older than the recorded capture time minus this margin are
# skipped when starting a shard from TRIM_HORIZON after a bulk copy.
_CAPTURE_MARGIN_S = 120


def _dynamo_hook(value: Any) -> tuple[Any, str] | None:
    if isinstance(value, set):
        items = list(value)
        if items and all(isinstance(i, decimal.Decimal) for i in items):
            return [float(i) for i in items], "NS"
        if items and all(isinstance(i, DynamoBinary) for i in items):
            import base64

            return [{"$binary": base64.b64encode(bytes(i.value)).decode("ascii")} for i in items], "BS"
        return [str(i) for i in items], "SS"
    if isinstance(value, DynamoBinary):
        import base64

        return {"$binary": base64.b64encode(bytes(value.value)).decode("ascii")}, "B"
    if isinstance(value, decimal.Decimal):
        # Integral values stay ints; others become floats, with the exact text
        # kept as a hint so a write-back doesn't lose precision.
        if value == value.to_integral_value() and abs(value) < 2**63:
            return int(value), "N"
        return float(value), "N"
    return None


def _deserialize_item(item: dict, types: dict[str, str] | None = None) -> dict:
    hints: dict[str, str] = {} if types is None else types
    raw = {k: _deserializer.deserialize(v) for k, v in item.items()}
    body = json_safe_typed(raw, _dynamo_hook, hints)
    # Plain numbers round-trip through TypeSerializer fine; keep only hints that matter.
    for path in [p for p, t in hints.items() if t == "N"]:
        del hints[path]
    return body


def _from_hint(value: Any, type_name: str) -> Any:
    if type_name == "SS":
        return set(str(v) for v in value)
    if type_name == "NS":
        return set(decimal.Decimal(str(v)) for v in value)
    if type_name == "BS":
        return set(decode_binary(v) for v in value)
    if type_name == "B":
        return decode_binary(value)
    return value


def _to_dynamo_value(value: Any) -> Any:
    """JSON value -> something TypeSerializer accepts (floats become Decimals)."""
    if isinstance(value, float):
        return decimal.Decimal(str(value))
    if isinstance(value, dict):
        if set(value.keys()) == {"$binary"}:
            return decode_binary(value)
        return {k: _to_dynamo_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo_value(v) for v in value]
    if isinstance(value, set):
        return {(_to_dynamo_value(v)) for v in value}
    return value


class DynamoDBConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._session: boto3.Session | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        return CdcCapabilities(
            method=CdcMethod.STREAM_SHARDS, resumable=True, captures_deletes=True, reverse_sync=True,
        )

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

        return self.describe_capabilities(SourceTopologySnapshot(
            source_type=SourceType.DYNAMODB,
            server_version=None,
            server_edition="Amazon DynamoDB",
            containers=containers,
            total_estimated_count=total_count,
            total_estimated_size_bytes=total_size or None,
            supports_cdc=any_stream_enabled,
            cdc_notes=" ".join(cdc_notes_parts) or None,
        ))

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
                            batch.append(_to_source_document(table, key_attrs, item))
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

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        now_ms = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        return {table: {"shards": {}, "finished": [], "captured_at_ms": now_ms} for table in containers}

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        client = self._client()
        streams = self._streams_client()
        q: queue.Queue = queue.Queue(maxsize=512)
        stop = threading.Event()
        lock = threading.Lock()

        # Per-table state: {"shards": {shard_id: last_sequence}, "finished": [...],
        # "captured_at_ms": int|None}. Older checkpoints were a bare
        # {shard_id: seq} map; accept that shape too.
        state: dict[str, dict[str, Any]] = {}
        for table in containers:
            raw = checkpoint.get(table) or {}
            if raw and "shards" not in raw:
                raw = {"shards": dict(raw), "finished": [], "captured_at_ms": None}
            state[table] = {
                "shards": dict(raw.get("shards") or {}),
                "finished": list(raw.get("finished") or []),
                "captured_at_ms": raw.get("captured_at_ms"),
            }
        key_attrs: dict[str, list[str]] = {}

        def _snapshot(table: str) -> dict[str, Any]:
            with lock:
                st = state[table]
                return {"shards": dict(st["shards"]), "finished": list(st["finished"])[-500:],
                        "captured_at_ms": st["captured_at_ms"]}

        def _read_shard(table: str, stream_arn: str, shard_id: str, on_done) -> None:
            try:
                with lock:
                    after = state[table]["shards"].get(shard_id)
                    captured_ms = state[table]["captured_at_ms"]
                if after:
                    it = streams.get_shard_iterator(
                        StreamArn=stream_arn, ShardId=shard_id,
                        ShardIteratorType="AFTER_SEQUENCE_NUMBER", SequenceNumber=after,
                    ).get("ShardIterator")
                else:
                    it = streams.get_shard_iterator(
                        StreamArn=stream_arn, ShardId=shard_id, ShardIteratorType="TRIM_HORIZON",
                    ).get("ShardIterator")
                min_ts = (captured_ms / 1000.0 - _CAPTURE_MARGIN_S) if (captured_ms and not after) else None
                while it and not stop.is_set():
                    resp = streams.get_records(ShardIterator=it, Limit=1000)
                    for record in resp.get("Records", []):
                        created = record.get("dynamodb", {}).get("ApproximateCreationDateTime")
                        created_s = created.timestamp() if hasattr(created, "timestamp") else created
                        if min_ts is not None and created_s is not None and created_s < min_ts:
                            with lock:
                                state[table]["shards"][shard_id] = record["dynamodb"]["SequenceNumber"]
                            continue
                        q.put((table, (shard_id, record)))
                    it = resp.get("NextShardIterator")
                    if not resp.get("Records"):
                        time.sleep(1.0)  # DynamoDB Streams recommends >=1s between empty polls
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "TrimmedDataAccessException":
                    q.put((table, SourceConnectorError(
                        f"DynamoDB Streams data for table '{table}' shard '{shard_id}' has aged out "
                        "(streams keep 24 hours). Changes were missed -- re-run the bulk copy for "
                        "this table, then continue syncing."
                    )))
                    return
                if code != "ResourceNotFoundException":
                    q.put((table, SourceConnectorError(
                        f"DynamoDB Streams read failed for table '{table}' shard '{shard_id}': {exc}"
                    )))
                    return
            except (BotoCoreError, Exception) as exc:  # noqa: BLE001
                q.put((table, SourceConnectorError(
                    f"DynamoDB Streams read failed for table '{table}' shard '{shard_id}': {exc}"
                )))
                return
            if not stop.is_set():
                on_done(shard_id)

        def _watch_table(table: str) -> None:
            try:
                desc = client.describe_table(TableName=table)["Table"]
                key_attrs[table] = [k["AttributeName"] for k in desc.get("KeySchema", [])]
                stream_arn = desc.get("LatestStreamArn")
                if not stream_arn:
                    q.put((table, SourceConnectorError(f"DynamoDB Streams is not enabled on table '{table}'.")))
                    return
                started: set[str] = set()

                def _done(shard_id: str) -> None:
                    with lock:
                        if shard_id not in state[table]["finished"]:
                            state[table]["finished"].append(shard_id)
                    q.put((table, ("_checkpoint", None)))

                while not stop.is_set():
                    shards = _describe_all_shards(streams, stream_arn)
                    present = {s["ShardId"] for s in shards}
                    with lock:
                        finished = set(state[table]["finished"])
                    for shard in shards:
                        sid = shard["ShardId"]
                        if sid in started or sid in finished:
                            continue
                        parent = shard.get("ParentShardId")
                        # Read a child only after its parent is exhausted (or has
                        # aged out of the stream), so per-item order is preserved.
                        if parent and parent in present and parent not in finished:
                            continue
                        started.add(sid)
                        threading.Thread(
                            target=_read_shard, args=(table, stream_arn, sid, _done), daemon=True,
                        ).start()
                    stop.wait(settings.dynamodb_shard_discovery_interval_s)
            except (BotoCoreError, ClientError) as exc:
                q.put((table, SourceConnectorError(f"DynamoDB Streams setup failed for table '{table}': {exc}")))

        for t in containers:
            threading.Thread(target=_watch_table, args=(t,), daemon=True).start()

        try:
            while True:
                try:
                    table, payload = await asyncio.to_thread(q.get, True, 2.0)
                except queue.Empty:
                    yield heartbeat()
                    continue
                if isinstance(payload, BaseException):
                    raise payload

                shard_id, record = payload
                if shard_id == "_checkpoint":
                    # A shard finished: persist that so a restart doesn't re-read it.
                    yield ChangeEvent(container=table, op="checkpoint", checkpoint=_snapshot(table))
                    continue
                event_name = record.get("eventName")
                ddb = record.get("dynamodb", {})
                with lock:
                    state[table]["shards"][shard_id] = ddb.get("SequenceNumber")
                cp = _snapshot(table)
                created = ddb.get("ApproximateCreationDateTime")
                source_ts = None
                if isinstance(created, _dt.datetime):
                    source_ts = created.astimezone(_dt.timezone.utc).replace(tzinfo=None)
                elif isinstance(created, (int, float)):
                    source_ts = _dt.datetime.utcfromtimestamp(created)
                attrs = key_attrs.get(table, [])

                if event_name in ("INSERT", "MODIFY") and ddb.get("NewImage"):
                    yield ChangeEvent(
                        container=table, op="upsert",
                        document=_to_source_document(table, attrs, ddb["NewImage"]),
                        checkpoint=cp, source_ts=source_ts,
                    )
                elif event_name in ("INSERT", "MODIFY"):
                    raise SourceConnectorError(
                        f"DynamoDB Streams on table '{table}' doesn't include item images. Set the "
                        "stream view type to NEW_IMAGE or NEW_AND_OLD_IMAGES."
                    )
                elif event_name == "REMOVE" and ddb.get("Keys"):
                    key_body = _deserialize_item(ddb["Keys"])
                    doc_key = _key_for(table, attrs or list(key_body.keys()), key_body)
                    yield ChangeEvent(container=table, op="delete", key=doc_key, checkpoint=cp, source_ts=source_ts)
        finally:
            stop.set()

    # -- reconciliation / fallback ----------------------------------------------

    async def list_keys(self, container: str) -> AsyncIterator[str]:
        client = self._client()
        attrs = await asyncio.to_thread(self._key_attrs, client, container)

        def _produce(q: queue.Queue) -> None:
            try:
                names = {f"#k{i}": a for i, a in enumerate(attrs)}
                kwargs: dict[str, Any] = {
                    "TableName": container,
                    "ProjectionExpression": ", ".join(names.keys()),
                    "ExpressionAttributeNames": names,
                }
                while True:
                    resp = client.scan(**kwargs)
                    q.put([_key_for(container, attrs, _deserialize_item(i)) for i in resp.get("Items", [])])
                    last = resp.get("LastEvaluatedKey")
                    if not last:
                        break
                    kwargs["ExclusiveStartKey"] = last
            except (BotoCoreError, ClientError) as exc:
                q.put(SourceConnectorError(f"DynamoDB key listing failed for '{container}': {exc}"))
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
        client = self._client()
        attrs = await asyncio.to_thread(self._key_attrs, client, container)
        if op == "delete":
            ident = dict(source_key or {})
            if not ident:
                raise SourceConnectorError(
                    f"Can't delete '{key}' from DynamoDB table '{container}': its key attributes are unknown."
                )
            item_key = {a: _serializer.serialize(_to_dynamo_value(ident[a])) for a in attrs if a in ident}
            await asyncio.to_thread(client.delete_item, TableName=container, Key=item_key)
            return
        doc = apply_type_hints(dict(body or {}), types, _from_hint)
        item = {k: _serializer.serialize(_to_dynamo_value(v)) for k, v in doc.items() if v is not None}
        await asyncio.to_thread(client.put_item, TableName=container, Item=item)


def _describe_all_shards(streams, stream_arn: str) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    start = None
    while True:
        kwargs: dict[str, Any] = {"StreamArn": stream_arn}
        if start:
            kwargs["ExclusiveStartShardId"] = start
        desc = streams.describe_stream(**kwargs)["StreamDescription"]
        shards.extend(desc.get("Shards", []))
        start = desc.get("LastEvaluatedShardId")
        if not start:
            return shards


def _to_source_document(table: str, key_attrs: list[str], raw_item: dict) -> SourceDocument:
    types: dict[str, str] = {}
    body = _deserialize_item(raw_item, types)
    return SourceDocument(
        key=_key_for(table, key_attrs, body), body=body, container=table,
        source_key={a: body.get(a) for a in key_attrs} if key_attrs else None,
        types=types or None,
    )


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
