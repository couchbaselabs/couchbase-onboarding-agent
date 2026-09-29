"""Redis type-aware reads and reverse writes against fakeredis."""
from __future__ import annotations

import pytest

fakeredis = pytest.importorskip("fakeredis")

from app.core.connectors.redis_connector import RedisConnector  # noqa: E402
from app.models.schemas import SourceConnectionConfig  # noqa: E402


@pytest.fixture
async def redis_conn():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server)
    conn = RedisConnector(SourceConnectionConfig(label="r", source_type="redis"))
    conn._client = fakeredis.FakeAsyncRedis(server=server)
    yield conn, client
    await conn.close()


async def test_extract_groups_by_prefix_and_keeps_types(redis_conn):
    conn, client = redis_conn
    await client.set("user:1", "alice")
    await client.hset("user:2", mapping={"name": "bob"})
    await client.rpush("queue:jobs", "a", "b")
    await client.expire("user:1", 300)
    docs: list = []

    async def sink(batch):
        docs.extend(batch)

    await conn.extract(["user", "queue"], sink)
    by_key = {d.key: d.body for d in docs}
    assert by_key["redis::user:1"]["value"] == "alice" and by_key["redis::user:1"]["ttl"]
    assert by_key["redis::user:2"]["value"] == {"name": "bob"}
    assert by_key["redis::queue:jobs"]["value"] == ["a", "b"]


async def test_reverse_write_round_trips(redis_conn):
    conn, client = redis_conn
    await conn.apply_reverse("user", "upsert", "redis::user:9",
                             {"redis_type": "hash", "redis_key": "user:9", "value": {"n": "1"}, "ttl": 60},
                             {"redis_key": "user:9"})
    assert await client.hgetall("user:9") == {b"n": b"1"}
    assert 0 < await client.ttl("user:9") <= 60
    await conn.apply_reverse("user", "delete", "redis::user:9", None, {"redis_key": "user:9"})
    assert await client.exists("user:9") == 0
