"""FastAPI application entrypoint for the Couchbase Onboarding Agent."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import agent, migrations, sources, stats
from app.api.security import auth_enabled, require_token
from app.config import get_settings
from app.core import runner
from app.core.connectors.registry import get_connector
from app.core.migration_engine import MigrationEngine, uses_xdcr
from app.core.store import MigrationStore
from app.models.enums import CONTINUOUS_STRATEGIES, COUCHBASE_SOURCE_TYPES, MigrationPhase, SourceType
from app.models.schemas import SourceConnectionConfig
from app.websocket.progress import broadcast_progress
from app.websocket.progress import router as ws_router

settings = get_settings()

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("onboarding_agent")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("%s %s starting up (env=%s)", settings.app_name, __version__, settings.environment)
    if not auth_enabled():
        logger.warning(
            "API_AUTH_TOKEN is not set: the API is open to anyone who can reach it. Set it for any "
            "deployment that isn't bound to localhost."
        )
    await _recover_interrupted_migrations()
    yield
    await runner.cancel_all()


app = FastAPI(
    title=settings.app_name,
    description="Migrate MongoDB, Amazon DynamoDB, Redis, Apache Cassandra, Azure Cosmos DB, or another "
    "Couchbase cluster (Community Edition, Enterprise Edition, or Capella) into Couchbase Server "
    "or Couchbase Capella -- bulk copy, continuous sync, rollback and post-cutover fallback.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_protected = [Depends(require_token)]
app.include_router(sources.router, prefix="/api/sources", tags=["sources"], dependencies=_protected)
app.include_router(migrations.router, prefix="/api/migrations", tags=["migrations"], dependencies=_protected)
app.include_router(stats.router, prefix="/api/stats", tags=["stats"], dependencies=_protected)
app.include_router(agent.router, prefix="/api/agent", tags=["agent"], dependencies=_protected)
app.include_router(ws_router, tags=["websocket"])


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": __version__}


@app.get("/api/config")
async def public_config() -> dict:
    """Unauthenticated: tells the UI whether to ask for an API token."""
    return {"auth_required": auth_enabled(), "version": __version__}


_TLS_FIELDS = ["use_tls", "ca_cert_pem", "tls_skip_verify"]

_SOURCE_TYPES: list[tuple[SourceType, str, list[str]]] = [
    (SourceType.MONGODB, "MongoDB", ["connection_string", "database", "username", "password", *_TLS_FIELDS]),
    (SourceType.DYNAMODB, "Amazon DynamoDB", [
        "aws_region", "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "dynamodb_endpoint_url",
    ]),
    (SourceType.REDIS, "Redis", ["connection_string", "redis_db_index", "username", "password", *_TLS_FIELDS]),
    (SourceType.CASSANDRA, "Apache Cassandra", [
        "connection_string", "cassandra_port", "cassandra_datacenter", "database", "username", "password",
        *_TLS_FIELDS,
    ]),
    (SourceType.COSMOSDB, "Microsoft Azure Cosmos DB", ["cosmos_endpoint", "cosmos_key", "database"]),
    (SourceType.COUCHBASE, "Couchbase (Community Edition)", [
        "connection_string", "database", "username", "password", *_TLS_FIELDS, "couchbase_external_network",
    ]),
    (SourceType.COUCHBASE_ENTERPRISE, "Couchbase (Enterprise Edition)", [
        "connection_string", "database", "username", "password", *_TLS_FIELDS, "couchbase_external_network",
    ]),
    (SourceType.COUCHBASE_CAPELLA, "Couchbase Capella", [
        "connection_string", "database", "username", "password", "ca_cert_pem", "tls_skip_verify",
    ]),
]


@app.get("/api/source-types", dependencies=_protected)
async def source_types() -> list[dict]:
    """Source-type picker metadata: connection fields per type, and what
    continuous sync gives you on each (method, resumable, deletes, fallback)."""
    out = []
    for value, label, fields in _SOURCE_TYPES:
        caps = get_connector(SourceConnectionConfig(label="", source_type=value)).capabilities
        out.append({
            "value": value.value, "label": label, "fields": fields,
            "cdc": {
                "method": caps.method.value if caps.method else None,
                "resumable": caps.resumable,
                "captures_deletes": caps.captures_deletes,
                "reverse_sync": caps.reverse_sync,
            },
        })
    return out


async def _recover_interrupted_migrations() -> None:
    """A restart kills the background tasks driving migrations. Resume the ones
    that can continue from their checkpoint; mark the rest failed with a clear
    reason rather than leaving them looking active."""
    store = MigrationStore.instance()
    engine = MigrationEngine(on_progress=broadcast_progress)
    for record in await store.list_all():
        phase = record.phase
        mid = str(record.migration_id)
        if phase == MigrationPhase.VALIDATING:
            record.phase = MigrationPhase.DRAFT
            record.log_tail.append("[startup] Validation was interrupted by a restart; run it again.")
            await store.save(record)
            continue

        if phase == MigrationPhase.FALLBACK_SYNCING and settings.auto_resume_on_startup:
            logger.info("Resuming fallback sync for migration %s", mid)
            runner.spawn(mid, "fallback", engine.run_fallback(record, fresh=False))
            continue

        resumable = phase in (MigrationPhase.MIGRATING, MigrationPhase.REPLICATING, MigrationPhase.VERIFYING) and (
            record.plan.strategy in CONTINUOUS_STRATEGIES
            or record.plan.source.source_type not in COUCHBASE_SOURCE_TYPES
            or phase == MigrationPhase.VERIFYING
        )
        if resumable and settings.auto_resume_on_startup:
            logger.info("Resuming migration %s (%s) after restart", mid, phase.value)
            runner.spawn(mid, "run", engine.resume_after_restart(record))
            continue

        if phase not in (
            MigrationPhase.MIGRATING, MigrationPhase.REPLICATING, MigrationPhase.VERIFYING,
            MigrationPhase.ROLLING_BACK, MigrationPhase.FALLBACK_SYNCING,
        ):
            continue
        message = (
            f"Migration was left in '{phase.value}' by a backend restart and wasn't resumed automatically."
        )
        if phase == MigrationPhase.ROLLING_BACK:
            message += " Run rollback again -- it continues from the last completed step."
        elif record.plan.strategy in CONTINUOUS_STRATEGIES:
            message += " Use Resume to continue from the last checkpoint."
        if uses_xdcr(record) and phase == MigrationPhase.REPLICATING:
            message += (
                " XDCR runs on the source cluster and may still be replicating; resuming picks the "
                "existing replication back up."
            )
        record.phase = MigrationPhase.FAILED
        record.error_message = message
        record.log_tail.append(f"[startup] {message}")
        await store.save(record)
        logger.warning("Marked migration %s failed after restart: %s", mid, message)
