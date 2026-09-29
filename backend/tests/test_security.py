from __future__ import annotations

import json
import os

from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.store import MigrationStore
from app.models.schemas import (
    CouchbaseConnectionConfig,
    MigrationPlanCreate,
    MigrationRecord,
    SourceConnectionConfig,
    redact_record,
)


def _record() -> MigrationRecord:
    return MigrationRecord(plan=MigrationPlanCreate(
        name="secret-test",
        source=SourceConnectionConfig(label="s", source_type="dynamodb", aws_access_key_id="AKIA",
                                      aws_secret_access_key="aws-secret-value"),
        destination=CouchbaseConnectionConfig(label="d", connection_string="couchbases://x", username="u",
                                              password="dest-password"),
        destination_bucket="b",
    ))


def test_redaction_masks_every_credential():
    data = json.dumps(redact_record(_record()))
    assert "aws-secret-value" not in data and "dest-password" not in data
    assert "AKIA" in data  # access key IDs aren't secret


async def test_state_file_is_encrypted_and_reloads():
    record = _record()
    await MigrationStore.instance().save(record)
    raw = open(os.environ["MIGRATION_STATE_FILE"]).read()
    assert "dest-password" not in raw and "aws-secret-value" not in raw and "enc:v1:" in raw
    MigrationStore._instance = None
    loaded = await MigrationStore.instance().get(record.migration_id)
    assert loaded.plan.destination.password == "dest-password"


def test_api_token_and_masked_responses(monkeypatch):
    from app.main import app

    settings = get_settings()
    monkeypatch.setattr(settings, "api_auth_token", "tok")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").json()["auth_required"] is True
        assert client.get("/api/migrations").status_code == 401
        headers = {"Authorization": "Bearer tok"}
        plan = _record().plan.model_dump(mode="json")
        created = client.post("/api/migrations", json=plan, headers=headers)
        assert created.status_code == 200
        body = created.text
        assert "dest-password" not in body and "aws-secret-value" not in body
        listed = client.get("/api/migrations", headers=headers).json()
        assert listed[0]["plan"]["destination"]["password"] == "********"
