"""FastAPI application entrypoint for the Couchbase Onboarding Agent."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import agent, migrations, sources, stats
from app.config import get_settings
from app.core.store import MigrationStore
from app.models.enums import COUCHBASE_SOURCE_TYPES, MigrationPhase
from app.websocket.progress import router as ws_router

settings = get_settings()

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("onboarding_agent")

app = FastAPI(
    title=settings.app_name,
    description="Dockerized AI agent for migrating MongoDB, Amazon DynamoDB, Redis, Apache "
    "Cassandra, Microsoft Azure Cosmos DB, or another Couchbase cluster (Community Edition, "
    "Enterprise Edition, or Capella) into Couchbase Server (Enterprise Edition) or Couchbase Capella.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(migrations.router, prefix="/api/migrations", tags=["migrations"])
app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(ws_router, tags=["websocket"])


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name}


@app.get("/api/source-types")
async def source_types() -> list[dict]:
    """Static metadata the wizard uses to render the source-type picker and to
    decide which connection-form fields to show for each of the eight source
    types (five non-Couchbase databases plus Couchbase Community/Enterprise/
    Capella as sources)."""
    return [
        {"value": "mongodb", "label": "MongoDB", "fields": [
            "connection_string", "database", "username", "password", "use_tls",
        ]},
        {"value": "dynamodb", "label": "Amazon DynamoDB", "fields": [
            "aws_region", "aws_access_key_id", "aws_secret_access_key", "aws_session_token",
            "dynamodb_endpoint_url",
        ]},
        {"value": "redis", "label": "Redis", "fields": [
            "connection_string", "redis_db_index", "username", "password", "use_tls",
        ]},
        {"value": "cassandra", "label": "Apache Cassandra", "fields": [
            "connection_string", "cassandra_port", "cassandra_datacenter", "database",
            "username", "password", "use_tls",
        ]},
        {"value": "cosmosdb", "label": "Microsoft Azure Cosmos DB", "fields": [
            "cosmos_endpoint", "cosmos_key", "database",
        ]},
        {"value": "couchbase", "label": "Couchbase (Community Edition)", "fields": [
            "connection_string", "database", "username", "password", "use_tls",
            "couchbase_external_network",
        ]},
        {"value": "couchbase_enterprise", "label": "Couchbase (Enterprise Edition)", "fields": [
            "connection_string", "database", "username", "password", "use_tls",
            "couchbase_external_network",
        ]},
        {"value": "couchbase_capella", "label": "Couchbase Capella", "fields": [
            "connection_string", "database", "username", "password",
        ]},
    ]


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("%s starting up (env=%s)", settings.app_name, settings.environment)
    await _reconcile_orphaned_migrations()


# Phases that only mean anything while a background asyncio task from a *previous*
# process is actively driving them (MigrationEngine.run_migration()'s background_tasks
# job, or its rollback/replication-stop counterparts). MigrationStore reloads whatever
# was last persisted to disk at startup (see core/store.py) with no memory of whether a
# task is still alive to drive it -- a backend restart mid-migration (container rebuild,
# crash, `docker compose up --build` while something was running) leaves these frozen at
# whatever phase was last saved, looking "still active" when nothing is actually
# progressing it anymore. Left alone, this produces confusing zombie state: the UI shows
# e.g. REPLICATING with stale-but-real-looking stats, and any action that assumes the
# phase is live (this app's own re-approval/restart guards, or a user retrying) can race
# against cluster-side state a fresh run doesn't expect -- confirmed on 2026-07-30, where
# a backend rebuild during an active XDCR replication led to a subsequent run failing with
# "cannot find remote cluster" for that same migration's own XDCR reference.
_ORPHANABLE_PHASES = (
    MigrationPhase.VALIDATING,
    MigrationPhase.MIGRATING,
    MigrationPhase.REPLICATING,
    MigrationPhase.VERIFYING,
    MigrationPhase.ROLLING_BACK,
)


async def _reconcile_orphaned_migrations() -> None:
    store = MigrationStore.instance()
    records = await store.list_all()
    for record in records:
        if record.phase not in _ORPHANABLE_PHASES:
            continue
        stale_phase = record.phase.value
        is_native = record.plan.source.source_type in COUCHBASE_SOURCE_TYPES
        record.phase = MigrationPhase.FAILED
        message = (
            f"Migration was left in '{stale_phase}' by a backend restart while it was still running "
            "(no background task survives a restart to keep driving it) -- marking it failed rather than "
            "leaving it stuck showing stale progress."
        )
        if is_native and stale_phase == MigrationPhase.REPLICATING.value:
            message += (
                " This was a Couchbase-source (XDCR) migration: XDCR replications run on the SOURCE "
                "cluster itself, independent of this app's process lifecycle, so it may still be "
                "actively replicating there. Check the source cluster's XDCR admin UI/REST API and "
                "tear it down manually if it's still running -- this app cannot safely assume whether "
                "to remove it automatically."
            )
        record.error_message = message
        record.log_tail.append(f"[startup reconciliation] {message}")
        await store.save(record)
        logger.warning("Reconciled orphaned migration %s: %s", record.migration_id, message)
