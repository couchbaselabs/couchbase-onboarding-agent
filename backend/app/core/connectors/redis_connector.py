"""
Redis source connector.

Redis has no native notion of "collections" the way the other four sources do, so
this connector groups keys into logical "containers" by convention: the segment of
each key before its first `:` (e.g. "user:123" -> container "user"), which is by
far the most common Redis key-naming pattern in practice. Keys with no `:` at all
land in a synthetic "(no-prefix)" container.

- test_connection(): server version, DBSIZE, a sampled SCAN to estimate per-prefix
  key counts, and whether `notify-keyspace-events` is configured (the CDC
  prerequisite).
- extract(): a full SCAN per container/prefix; each key's value is read with the
  command appropriate to its type (GET/HGETALL/LRANGE/SMEMBERS/ZRANGE/XRANGE) and
  wrapped in a small envelope that preserves the Redis type and TTL.
- stream_changes(): Redis keyspace notifications (pub/sub). Pub/sub has no
  backlog, so this connector never relies on replaying it. Instead it keeps a
  subscription open and records which keys changed ("dirty keys"), then re-reads
  each dirty key's current value -- a missing key becomes a delete. That makes
  the stream gap-free in the two places a plain subscriber loses data:
    * During a bulk copy: capture_change_position() subscribes before the copy
      starts, and the dirty keys are re-read once it finishes.
    * After a restart or dropped connection: with no subscription to trust, the
      stream subscribes first and then re-scans every key before going live.
  Past REDIS_DIRTY_KEY_LIMIT distinct keys it re-scans instead of re-reading.
  Redis Cluster publishes notifications per node; point this connector at a
  single primary (or a non-clustered endpoint).

This connector uses redis-py's native asyncio client throughout rather than the
thread+queue bridge the other (synchronous-SDK) connectors use -- redis-py has
first-class asyncio support, so no bridging is needed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.connectors.base import (
    CdcCapabilities,
    ChangeEvent,
    ReverseBatchResult,
    ReverseOp,
    SourceConnector,
    SourceConnectorError,
    SourceDocument,
    heartbeat,
)
from app.core.connectors.util import decode_binary, json_safe, make_key
from app.core.tls import ca_file_for
from app.models.enums import CdcMethod, SourceType
from app.models.schemas import SourceConnectionConfig, SourceContainerStats, SourceTopologySnapshot

logger = logging.getLogger(__name__)

NO_PREFIX = "(no-prefix)"


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return json_safe(value)  # base64-wrapped, see util.json_safe
    return value


def _prefix_of(key: str) -> str:
    return key.split(":", 1)[0] if ":" in key else NO_PREFIX


def _doc_key(redis_key: str) -> str:
    return make_key("redis", redis_key)


class _DirtyKeyTracker:
    """Holds a keyevent subscription open and remembers which keys changed.
    Runs as a background task on the connector's event loop."""

    def __init__(self, client: aioredis.Redis, db_index: int, containers: set[str], limit: int):
        self._client = client
        self._db_index = db_index
        self._containers = containers
        self._limit = limit
        self._keys: dict[str, None] = {}
        self._pubsub = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self.overflowed = False
        self.error: BaseException | None = None

    async def start(self) -> None:
        self._pubsub = self._client.pubsub()
        await self._pubsub.psubscribe(f"__keyevent@{self._db_index}__:*")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    continue
                key = _decode(message["data"])
                if not isinstance(key, str) or _prefix_of(key) not in self._containers:
                    continue
                if self.overflowed:
                    continue
                self._keys[key] = None
                if len(self._keys) > self._limit:
                    self.overflowed = True
                    self._keys.clear()
                self._changed.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            self._changed.set()

    def drain(self, max_keys: int = 1000) -> list[str]:
        out: list[str] = []
        for key in list(self._keys.keys())[:max_keys]:
            out.append(key)
            del self._keys[key]
        if not self._keys:
            self._changed.clear()
        return out

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except TimeoutError:
            pass

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._pubsub is not None:
            try:
                await self._pubsub.punsubscribe()
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass


class RedisConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._client: aioredis.Redis | None = None
        self._tracker: _DirtyKeyTracker | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

    @property
    def capabilities(self) -> CdcCapabilities:
        # Not resumable in the log sense -- a restart re-scans (see module docstring).
        return CdcCapabilities(
            method=CdcMethod.KEYSPACE_EVENTS, resumable=False, captures_deletes=True, reverse_sync=True,
        )

    def _connect(self) -> aioredis.Redis:
        if self._client is not None:
            return self._client
        conn_str = self.config.connection_string or "redis://localhost:6379"
        kwargs: dict[str, Any] = {"db": self.config.redis_db_index, "socket_timeout": 15}
        if self.config.username:
            kwargs["username"] = self.config.username
        if self.config.password:
            kwargs["password"] = self.config.password
        if self.config.use_tls:
            kwargs["ssl"] = True
            ca_file = ca_file_for(self.config)
            if ca_file:
                kwargs["ssl_ca_certs"] = ca_file
            if self.config.tls_skip_verify:
                kwargs["ssl_cert_reqs"] = "none"
        self._client = aioredis.from_url(conn_str, **kwargs)
        return self._client

    async def close(self) -> None:
        if self._tracker is not None:
            await self._tracker.close()
            self._tracker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- introspection --------------------------------------------------------

    async def test_connection(self) -> SourceTopologySnapshot:
        client = self._connect()
        try:
            await client.ping()
        except RedisError as exc:
            raise SourceConnectorError(f"Could not connect to Redis ({self.config.label}): {exc}") from exc

        info = await client.info()
        server_version = info.get("redis_version", "unknown")
        dbsize = int(await client.dbsize())

        notify_cfg = ""
        try:
            cfg = await client.config_get("notify-keyspace-events")
            notify_cfg = _decode(cfg.get("notify-keyspace-events", "")) or ""
        except RedisError:
            pass
        # Continuous sync needs both a "K" (keyspace) or "E" (keyevent) class flag
        # -- this connector subscribes to __keyevent@<db>__:* channels, which
        # requires the "E" class plus at least one operation class (g$lshzxet...).
        supports_cdc = "E" in notify_cfg
        if supports_cdc:
            cdc_notes = (
                "Uses Redis keyspace notifications to track which keys change, then re-reads them. "
                "Notifications aren't durable, so after a restart the sync re-scans every key "
                "before going live again -- no changes are lost, but expect a re-scan's worth of "
                "reads."
            )
        else:
            cdc_notes = (
                "notify-keyspace-events is not configured for keyevent notifications. Run "
                "`CONFIG SET notify-keyspace-events KEA` on the source (or a narrower class "
                "selection) to enable continuous sync."
            )

        prefix_counts: dict[str, int] = {}
        prefix_samples: dict[str, list[str]] = {}
        sample_size = 0
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, count=1000)
            for raw_key in keys:
                key_str = _decode(raw_key)
                prefix = _prefix_of(key_str)
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
                samples = prefix_samples.setdefault(prefix, [])
                if len(samples) < 5:
                    samples.append(key_str)
            sample_size += len(keys)
            if cursor == 0 or sample_size >= 20000:
                break

        scale = (dbsize / sample_size) if sample_size and dbsize else 1.0
        containers = [
            SourceContainerStats(
                name=prefix, estimated_count=round(count * scale),
                sample_fields=prefix_samples.get(prefix, []),
                notes=["Estimated by sampling a SCAN, not an exact count."],
            )
            for prefix, count in sorted(prefix_counts.items(), key=lambda kv: -kv[1])[:200]
        ]

        return self.describe_capabilities(SourceTopologySnapshot(
            source_type=SourceType.REDIS,
            server_version=server_version,
            server_edition=f"Redis {server_version} (logical db {self.config.redis_db_index})",
            containers=containers,
            total_estimated_count=dbsize,
            total_estimated_size_bytes=None,  # Redis has no cheap per-key size accounting
            supports_cdc=supports_cdc,
            cdc_notes=cdc_notes,
        ))

    async def _read_keys(self, client: aioredis.Redis, keys: list[str]) -> dict[str, dict | None]:
        """Read many keys with two pipelined round trips (TYPE+PTTL, then the
        type-appropriate read) instead of three per key. Returns None for keys
        that no longer exist."""
        if not keys:
            return {}
        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.type(key)
            pipe.pttl(key)
        meta = await pipe.execute()
        kinds = {k: _decode(meta[i * 2]) for i, k in enumerate(keys)}
        ttls = {k: meta[i * 2 + 1] for i, k in enumerate(keys)}

        pipe = client.pipeline(transaction=False)
        readable: list[str] = []
        for key in keys:
            kind = kinds[key]
            if kind == "string":
                pipe.get(key)
            elif kind == "hash":
                pipe.hgetall(key)
            elif kind == "list":
                pipe.lrange(key, 0, -1)
            elif kind == "set":
                pipe.smembers(key)
            elif kind == "zset":
                pipe.zrange(key, 0, -1, withscores=True)
            elif kind == "stream":
                pipe.xrange(key, count=10000)
            else:
                continue  # "none" (vanished) or an unsupported module type
            readable.append(key)
        values = await pipe.execute() if readable else []

        out: dict[str, dict | None] = {k: None for k in keys}
        for key, raw in zip(readable, values, strict=True):
            kind = kinds[key]
            if raw is None:
                continue
            if kind == "string":
                value: Any = _decode(raw)
            elif kind == "hash":
                value = {_decode(k): _decode(v) for k, v in raw.items()}
            elif kind in ("list", "set"):
                value = [_decode(v) for v in raw]
            elif kind == "zset":
                value = [{"member": _decode(m), "score": sc} for m, sc in raw]
            else:
                value = [
                    {"id": _decode(entry_id), "fields": {_decode(k): _decode(v) for k, v in fields.items()}}
                    for entry_id, fields in raw
                ]
            pttl = ttls.get(key)
            ttl = max(1, int(pttl) // 1000) if isinstance(pttl, int) and pttl > 0 else None
            out[key] = {"redis_type": kind, "redis_key": key, "value": value, "ttl": ttl}
        return out

    async def _read_key(self, client: aioredis.Redis, key: str) -> dict | None:
        return (await self._read_keys(client, [key])).get(key)

    def _documents(self, container: str, docs: dict[str, dict | None]) -> list[SourceDocument]:
        return [
            SourceDocument(key=_doc_key(k), body=v, container=container, source_key={"redis_key": k})
            for k, v in docs.items() if v is not None
        ]

    async def _scan_container(self, client: aioredis.Redis, container: str, count: int) -> AsyncIterator[list[str]]:
        pattern = f"{container}:*" if container != NO_PREFIX else "*"
        cursor = 0
        while True:
            cursor, raw_keys = await client.scan(cursor=cursor, match=pattern, count=count)
            keys = [_decode(k) for k in raw_keys]
            if container == NO_PREFIX:
                keys = [k for k in keys if ":" not in k]
            if keys:
                yield keys
            if cursor == 0:
                break

    # -- extraction -------------------------------------------------------------

    async def extract(
        self,
        containers: list[str],
        sink: Callable[[list[SourceDocument]], Awaitable[None]],
        *,
        batch_size: int = 500,
    ) -> None:
        client = self._connect()
        for container in containers:
            batch: list[SourceDocument] = []
            async for keys in self._scan_container(client, container, batch_size):
                batch.extend(self._documents(container, await self._read_keys(client, keys)))
                if len(batch) >= batch_size:
                    await sink(batch)
                    batch = []
            if batch:
                await sink(batch)

    # -- change data capture -----------------------------------------------------

    async def _start_tracker(self, containers: list[str]) -> _DirtyKeyTracker:
        if self._tracker is not None:
            await self._tracker.close()
        tracker = _DirtyKeyTracker(
            self._connect(), self.config.redis_db_index, set(containers),
            get_settings().redis_dirty_key_limit,
        )
        await tracker.start()
        self._tracker = tracker
        return tracker

    async def capture_change_position(self, containers: list[str]) -> dict[str, Any]:
        await self._start_tracker(containers)
        # Marker only: its presence tells a restarted backend that a subscription
        # existed before, so it must re-scan rather than trust a new one.
        return {c: {"tracking": True} for c in containers}

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        client = self._connect()
        tracker = self._tracker
        rescan = tracker is None
        if tracker is None:
            # No subscription survived from before (restart, reconnect, or CDC
            # starting fresh here): subscribe first, then re-read everything.
            tracker = await self._start_tracker(containers)
        marker = {"tracking": True}

        while True:
            if tracker.error is not None:
                raise SourceConnectorError(f"Redis keyspace subscription failed: {tracker.error}")
            if rescan or tracker.overflowed:
                if tracker.overflowed:
                    logger.warning("Redis dirty-key limit exceeded; re-scanning all keys.")
                    tracker.overflowed = False
                rescan = False
                for container in containers:
                    async for keys in self._scan_container(client, container, 500):
                        for doc in self._documents(container, await self._read_keys(client, keys)):
                            yield ChangeEvent(container=container, op="upsert", document=doc, checkpoint=marker)
                continue

            keys = tracker.drain(500)
            if not keys:
                await tracker.wait(2.0)
                if not tracker._keys and tracker.error is None:  # noqa: SLF001
                    yield heartbeat()
                continue
            docs = await self._read_keys(client, keys)
            for key, doc in docs.items():
                container = _prefix_of(key)
                if doc is None:
                    yield ChangeEvent(container=container, op="delete", key=_doc_key(key), checkpoint=marker)
                else:
                    yield ChangeEvent(
                        container=container, op="upsert", checkpoint=marker,
                        document=SourceDocument(
                            key=_doc_key(key), body=doc, container=container, source_key={"redis_key": key},
                        ),
                    )

    # -- reconciliation / fallback ----------------------------------------------

    async def list_keys(self, container: str) -> AsyncIterator[str]:
        client = self._connect()
        async for keys in self._scan_container(client, container, 1000):
            for key in keys:
                yield _doc_key(key)

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
        pipe = client.pipeline(transaction=True)
        self._queue_reverse(pipe, ReverseOp(op=op, key=key, body=body, source_key=source_key, types=types))
        await pipe.execute()

    async def apply_reverse_batch(self, container: str, ops: list[ReverseOp]) -> ReverseBatchResult:
        """One pipelined round trip for the whole batch (non-transactional, so
        a single bad key doesn't abort the rest; failures are reported per key)."""
        result = ReverseBatchResult()
        if not ops:
            return result
        client = self._connect()
        pipe = client.pipeline(transaction=False)
        counts: list[int] = []
        for op in ops:
            before = len(pipe.command_stack)
            self._queue_reverse(pipe, op)
            counts.append(len(pipe.command_stack) - before)
        try:
            replies = await pipe.execute(raise_on_error=False)
        except RedisError as exc:
            result.failed.extend((op.key, str(exc)) for op in ops)
            return result
        pos = 0
        for op, n in zip(ops, counts, strict=True):
            errors = [r for r in replies[pos:pos + n] if isinstance(r, Exception)]
            pos += n
            if errors:
                result.failed.append((op.key, str(errors[0])))
            else:
                result.applied += 1
        return result

    @staticmethod
    def _queue_reverse(pipe, op: ReverseOp) -> None:
        redis_key = (op.source_key or {}).get("redis_key") or (op.body or {}).get("redis_key")
        if not redis_key:
            redis_key = op.key.split("::", 1)[1] if op.key.startswith("redis::") else op.key
        if op.op == "delete":
            pipe.delete(redis_key)
            return
        body = op.body or {}
        kind = body.get("redis_type", "string")
        value = body.get("value")
        pipe.delete(redis_key)
        if kind == "string":
            value = decode_binary(value)
            pipe.set(redis_key, value if isinstance(value, (str, bytes, int, float)) else str(value))
        elif kind == "hash" and value:
            pipe.hset(redis_key, mapping={str(k): v for k, v in value.items()})
        elif kind == "list" and value:
            pipe.rpush(redis_key, *value)
        elif kind == "set" and value:
            pipe.sadd(redis_key, *value)
        elif kind == "zset" and value:
            pipe.zadd(redis_key, {str(e["member"]): float(e["score"]) for e in value})
        elif kind == "stream" and value:
            for entry in value:
                pipe.xadd(redis_key, entry.get("fields") or {"_": ""}, id=entry.get("id", "*"))
        ttl = body.get("ttl")
        if ttl:
            pipe.expire(redis_key, int(ttl))
