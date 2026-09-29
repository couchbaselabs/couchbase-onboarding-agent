"""DynamoDB bulk copy + Streams catch-up against moto's in-process AWS."""
from __future__ import annotations

import asyncio
import os

import boto3
import pytest

moto = pytest.importorskip("moto")

from app.core.connectors.dynamodb import DynamoDBConnector  # noqa: E402
from app.models.schemas import SourceConnectionConfig  # noqa: E402


@pytest.fixture
def aws():
    os.environ.update({
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1",
    })
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="orders",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        )
        yield client


def _connector() -> DynamoDBConnector:
    return DynamoDBConnector(SourceConnectionConfig(label="ddb", source_type="dynamodb", aws_region="us-east-1"))


async def test_bulk_copy_then_stream_sees_changes_made_after_capture(aws):
    for i in range(3):
        aws.put_item(TableName="orders", Item={"pk": {"S": f"o{i}"}, "total": {"N": str(i * 10)},
                                               "tags": {"SS": ["a", "b"]}})
    conn = _connector()
    topo = await conn.test_connection()
    assert topo.supports_cdc and topo.cdc_resumable and topo.cdc_captures_deletes

    position = await conn.capture_change_position(["orders"])
    copied: list = []

    async def sink(batch):
        copied.extend(batch)

    await conn.extract(["orders"], sink, batch_size=2)
    assert sorted(d.key for d in copied) == ["orders::o0", "orders::o1", "orders::o2"]
    assert copied[0].types and any(t == "SS" for t in copied[0].types.values())

    aws.put_item(TableName="orders", Item={"pk": {"S": "o9"}, "total": {"N": "90"}})
    aws.delete_item(TableName="orders", Key={"pk": {"S": "o1"}})

    seen: dict[str, str] = {}

    async def consume():
        async for event in conn.stream_changes(["orders"], position):
            if event.op == "upsert":
                seen[event.document.key] = "upsert"
            elif event.op == "delete":
                seen[event.key] = "delete"
            if seen.get("orders::o9") == "upsert" and seen.get("orders::o1") == "delete":
                return

    await asyncio.wait_for(consume(), timeout=30)
    await conn.close()


async def test_reverse_write_restores_native_types(aws):
    conn = _connector()
    await conn.apply_reverse(
        "orders", "upsert", "orders::x1", {"pk": "x1", "tags": ["a", "b"], "total": 12.5},
        {"pk": "x1"}, {"/tags": "SS"},
    )
    item = aws.get_item(TableName="orders", Key={"pk": {"S": "x1"}})["Item"]
    assert item["tags"] == {"SS": ["a", "b"]} or set(item["tags"]["SS"]) == {"a", "b"}
    assert item["total"] == {"N": "12.5"}
    await conn.apply_reverse("orders", "delete", "orders::x1", None, {"pk": "x1"}, None)
    assert "Item" not in aws.get_item(TableName="orders", Key={"pk": {"S": "x1"}})
