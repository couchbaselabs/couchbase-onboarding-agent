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
- stream_changes(): Redis keyspace notifications (pub/sub). LIGHTER-DEPTH
  IMPLEMENTATION (see README's connector-depth notes): this is Redis's only
  built-in change-notification mechanism short of running as a replica speaking
  the replication protocol, and it is fundamentally NOT durable -- pub/sub has no
  backlog, so any event published while this app isn't actively subscribed
  (restart, network blip, backpressure) is lost forever, not merely delayed. There
  is no resumable checkpoint for this reason; the sibling four connectors' CDC
  mechanisms are all resumable logs, this one is not, and that gap is inherent to
  Redis's own notification model rather than a shortcut taken here.

This connector uses redis-py's native asyncio client throughout rather than the
thread+queue bridge the other (synchronous-SDK) connectors use -- redis-py has
first-class asyncio support, so no bridging is needed.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.connectors.base import ChangeEvent, SourceConnector, SourceConnectorError, SourceDocument
from app.core.connectors.util import json_safe, make_key
from app.models.enums import SourceType
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


class RedisConnector(SourceConnector):
    def __init__(self, config: SourceConnectionConfig):
        super().__init__(config)
        self._client: aioredis.Redis | None = None

    @property
    def supports_cdc(self) -> bool:
        return True

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
            if self.config.ca_cert_path:
                kwargs["ssl_ca_certs"] = self.config.ca_cert_path
        self._client = aioredis.from_url(conn_str, **kwargs)
        return self._client

    async def close(self) -> None:
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
                "Uses Redis keyspace notifications (pub/sub) for continuous sync. This is "
                "best-effort and NOT durable -- events published while this app isn't actively "
                "subscribed (restart, network blip) are lost, unlike the other four connectors' "
                "resumable CDC mechanisms."
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

        return SourceTopologySnapshot(
            source_type=SourceType.REDIS,
            server_version=server_version,
            server_edition=f"Redis {server_version} (logical db {self.config.redis_db_index})",
            containers=containers,
            total_estimated_count=dbsize,
            total_estimated_size_bytes=None,  # Redis has no cheap per-key size accounting
            supports_cdc=supports_cdc,
            cdc_notes=cdc_notes,
        )

    async def _read_key(self, client: aioredis.Redis, key: str) -> dict | None:
        key_type = _decode(await client.type(key))
        ttl = await client.ttl(key)
        value: Any
        if key_type == "string":
            value = _decode(await client.get(key))
        elif key_type == "hash":
            raw = await client.hgetall(key)
            value = {_decode(k): _decode(v) for k, v in raw.items()}
        elif key_type == "list":
            value = [_decode(v) for v in await client.lrange(key, 0, -1)]
        elif key_type == "set":
            value = [_decode(v) for v in await client.smembers(key)]
        elif key_type == "zset":
            raw = await client.zrange(key, 0, -1, withscores=True)
            value = [{"member": _decode(m), "score": s} for m, s in raw]
        elif key_type == "stream":
            raw = await client.xrange(key, count=10000)
            value = [
                {"id": _decode(entry_id), "fields": {_decode(k): _decode(v) for k, v in fields.items()}}
                for entry_id, fields in raw
            ]
        else:
            return None  # "none" (key vanished between SCAN and read) or an unsupported module type
        return {"redis_type": key_type, "redis_key": key, "value": value, "ttl": ttl if ttl and ttl > 0 else None}

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
            pattern = f"{container}:*" if container != NO_PREFIX else "*"
            batch: list[SourceDocument] = []
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=batch_size)
                for raw_key in keys:
                    key_str = _decode(raw_key)
                    if container == NO_PREFIX and ":" in key_str:
                        continue
                    doc = await self._read_key(client, key_str)
                    if doc is None:
                        continue
                    batch.append(SourceDocument(key=make_key("redis", key_str), body=doc, container=container))
                    if len(batch) >= batch_size:
                        await sink(batch)
                        batch = []
                if cursor == 0:
                    break
            if batch:
                await sink(batch)

    # -- change data capture -----------------------------------------------------

    async def stream_changes(
        self, containers: list[str], checkpoint: dict[str, Any],
    ) -> AsyncIterator[ChangeEvent]:
        client = self._connect()
        pubsub = client.pubsub()
        db_index = self.config.redis_db_index
        container_set = set(containers)
        await pubsub.psubscribe(f"__keyevent@{db_index}__:*")
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
                if message is None:
                    yield ChangeEvent(container="*", op="heartbeat")
                    continue
                channel = _decode(message["channel"])
                # channel looks like "__keyevent@0__:set" / "__keyevent@0__:del" / ...
                event = channel.rsplit(":", 1)[-1]
                key = _decode(message["data"])
                prefix = _prefix_of(key)
                if prefix not in container_set:
                    continue
                if event in ("del", "expired", "evicted"):
                    yield ChangeEvent(container=prefix, op="delete", key=make_key("redis", key))
                    continue
                doc = await self._read_key(client, key)
                if doc is None:
                    yield ChangeEvent(container=prefix, op="delete", key=make_key("redis", key))
                else:
                    yield ChangeEvent(
                        container=prefix, op="upsert",
                        document=SourceDocument(key=make_key("redis", key), body=doc, container=prefix),
                    )
        finally:
            await pubsub.punsubscribe()
            await pubsub.aclose()
