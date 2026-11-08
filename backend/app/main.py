"""FastAPI application entrypoint for the Couchbase Onboarding Agent."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import agent, auth, migrations, settings_security, sources, stats
from app.api.security import auth_enabled, require_token
from app.config import get_settings
from app.core import runner
from app.core.auth_store import AuthStore
from app.core.certs import TrustStore
from app.core.connectors.registry import get_connector
from app.core.ldap_auth import LdapStore
from app.core.llm_settings import LlmSettingsStore
from app.core.log_forwarding import Forwarder, ForwardStore
from app.core.migration_engine import MigrationEngine, uses_xdcr
from app.core.store import MigrationStore
from app.models.enums import CONTINUOUS_STRATEGIES, COUCHBASE_SOURCE_TYPES, MigrationPhase, SourceType, SyncStage
from app.models.schemas import SourceConnectionConfig
from app.websocket.progress import broadcast_progress
from app.websocket.progress import router as ws_router

settings = get_settings()

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("onboarding_agent")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("%s %s starting up (env=%s)", settings.app_name, __version__, settings.environment)
    for warning in startup_security_warnings():
        logger.warning(warning)
    TrustStore.instance()  # exports the uploaded-CA bundle to SSL_CERT_FILE & co.
    Forwarder.instance().apply(ForwardStore.instance().config)  # Settings -> Log forwarding
    await _recover_interrupted_migrations()
    yield
    await runner.cancel_all()


def startup_security_warnings() -> list[str]:
    """Weak or default configuration worth shouting about at boot (PCI DSS
    8.3.1 "no vendor defaults"; NIST CM-6). Warnings, not hard failures: a
    single-user laptop run is a supported mode."""
    out: list[str] = []
    if not auth_enabled():
        out.append(
            "ADMIN_LOGIN_ENABLED=false and API_AUTH_TOKEN is not set: the API is open to anyone who can "
            "reach it. Only run like this bound to localhost."
        )
    elif AuthStore.instance().setup_required and not settings.api_auth_token:
        out.append(
            "No administrator password yet: the first visitor to the UI will be asked to set one. Set "
            "API_AUTH_TOKEN as well so that setting it requires the token (recommended anywhere but localhost)."
        )
    llm = LlmSettingsStore.instance().effective()
    if settings.llm_enabled and not llm.is_local:
        out.append(
            f"The assistant uses a hosted model ({llm.label}, '{llm.effective_model}'): validation messages, "
            "migration stats and chat questions are sent to that provider. Database credentials never are."
        )
    elif settings.api_auth_token and len(settings.api_auth_token) < 32:
        out.append(
            "API_AUTH_TOKEN is shorter than 32 characters. Use a random token of at least 32 characters "
            "(e.g. `openssl rand -hex 32`); the API grants access to every stored database credential."
        )
    if settings.memory_cb_password in ("password", "", "CouchbaseDemo123!"):
        out.append(
            "MEMORY_CB_PASSWORD is the built-in default. Set a strong password in .env -- it is also the "
            "memory Couchbase container's administrator password."
        )
    if settings.environment != "development" and not settings.state_encryption_key:
        out.append(
            "STATE_ENCRYPTION_KEY is not set: the key that encrypts stored credentials is generated into the "
            "state volume, so a copy of that volume can decrypt them. Set it explicitly (e.g. from a Secret)."
        )
    if settings.api_docs_enabled and settings.environment != "development":
        out.append("ENABLE_API_DOCS is on outside development: /docs, /redoc and /openapi.json are served unauthenticated.")
    if settings.cors_origin_list and settings.environment != "development":
        out.append(
            f"CORS_ORIGINS allows cross-origin API calls from {settings.cors_origin_list}. The bundled UI is "
            "served same-origin and doesn't need this; keep the list to origins you control."
        )
    return out


app = FastAPI(
    title=settings.app_name,
    description="Migrate MongoDB, Amazon DynamoDB, Redis, Apache Cassandra, Azure Cosmos DB, or another "
    "Couchbase cluster (Community Edition, Enterprise Edition, or Capella) into Couchbase Server "
    "or Couchbase Capella -- bulk copy, continuous sync, multi-bucket/scope/collection targets, rollback, "
    "post-cutover fallback and failback (bi-directional replication).",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)

_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
# The API only ever returns JSON (and websocket upgrades): nothing should
# render it in a frame, run it as a script, or cache it -- responses carry
# redacted but still sensitive migration state.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.path not in _DOCS_PATHS:
        # Swagger/ReDoc load their assets from a CDN; a strict CSP there breaks the page.
        headers.setdefault("Content-Security-Policy", _API_CSP)
    if request.url.path.startswith("/api/"):
        headers.setdefault("Cache-Control", "no-store")
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if request.url.scheme == "https" or forwarded_proto.split(",")[0].strip() == "https":
        # Only meaningful once the request actually travelled over TLS (the
        # bundled stack terminates TLS at an ingress/reverse proxy, if at all).
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

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
app.include_router(settings_security.router, prefix="/api/settings", tags=["settings"], dependencies=_protected)
# Login endpoints issue the bearer, so they can't require one; each handler
# enforces what it needs (api/routes/auth.py).
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(ws_router, tags=["websocket"])


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": __version__}


@app.get("/api/config")
async def public_config() -> dict:
    """Unauthenticated: tells the UI whether to log in (and whether the
    administrator password still has to be set), and whether the assistant is
    available in this deployment."""
    store = AuthStore.instance()
    return {
        "auth_required": auth_enabled(),
        "login_enabled": store.login_enabled,
        "setup_required": store.setup_required,
        # The first-run setup form must also ask for API_AUTH_TOKEN when one is configured.
        "bootstrap_token_required": store.setup_required and bool(settings.api_auth_token),
        "version": __version__,
        "llm_enabled": settings.llm_enabled,
        # Directory sign-in is on: the login form says so (local or directory account).
        "ldap_enabled": LdapStore.instance().enabled,
    }


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

        # Only resume what this version started (it has checkpoint and rollback
        # state). A record left "running" by an older version is marked failed
        # instead, so an upgrade never silently reconnects to old clusters.
        started_by_this_version = record.rollback_state.captured or record.sync.stage != SyncStage.IDLE
        resumable = started_by_this_version and phase in (
            MigrationPhase.MIGRATING, MigrationPhase.REPLICATING, MigrationPhase.VERIFYING,
        ) and (
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
                " XDCR runs on the source cluster and may still be replicating there; check the source "
                "cluster's XDCR page and remove the replication if you don't resume."
            )
        record.phase = MigrationPhase.FAILED
        record.error_message = message
        record.log_tail.append(f"[startup] {message}")
        await store.save(record)
        logger.warning("Marked migration %s failed after restart: %s", mid, message)
