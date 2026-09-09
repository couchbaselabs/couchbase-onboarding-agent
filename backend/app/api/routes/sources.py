"""Endpoints for testing/introspecting a source database connection and the
Couchbase destination connection -- used by the wizard's 'Test & introspect
source' and 'Test destination connection' buttons."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.core.connectors.base import SourceConnectorError
from app.core.connectors.registry import get_connector
from app.core.couchbase_client import CouchbaseClientError, CouchbaseClusterClient
from app.models.schemas import CouchbaseConnectionConfig, CouchbaseTopologySnapshot, SourceConnectionConfig, SourceTopologySnapshot

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/test-connection", response_model=SourceTopologySnapshot)
async def test_source_connection(config: SourceConnectionConfig) -> SourceTopologySnapshot:
    connector = get_connector(config)
    try:
        return await connector.test_connection()
    except SourceConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Unexpected error connecting to source: {exc}") from exc
    finally:
        await connector.close()


@router.post("/test-destination", response_model=CouchbaseTopologySnapshot)
async def test_destination_connection(config: CouchbaseConnectionConfig) -> CouchbaseTopologySnapshot:
    client = CouchbaseClusterClient(config)
    try:
        return client.snapshot_topology()
    except CouchbaseClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        client.close()
