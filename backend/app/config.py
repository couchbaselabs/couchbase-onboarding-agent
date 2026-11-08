"""Central configuration, loaded from environment variables (see env.example)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- App ---
    app_name: str = "Couchbase Labs Onboarding Agent"
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed cross-origin callers. The default is the Vite dev server's
        origins and applies only in development; everywhere else the UI is
        served same-origin (nginx proxies /api and /ws), so the list is empty
        unless CORS_ORIGINS is set explicitly."""
        default = type(self).model_fields["cors_origins"].default
        if self.environment != "development" and self.cors_origins == default:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def api_docs_enabled(self) -> bool:
        if self.enable_api_docs is not None:
            return self.enable_api_docs
        return self.environment == "development"

    # --- Security ---
    # When set, every /api and /ws request must present this token (Authorization:
    # Bearer <token>, or ?token= for websockets). Leave empty only for a local,
    # single-user run bound to 127.0.0.1 -- the API can read production credentials
    # and delete destination data.
    api_auth_token: str = ""
    # Brute-force throttling for the token check (per source IP): this many
    # failures inside the window locks the IP out for auth_lockout_s.
    auth_max_failures: int = 10
    auth_failure_window_s: int = 15 * 60
    auth_lockout_s: int = 30 * 60
    # Read the client IP from X-Forwarded-For (first hop). On by default because
    # the bundled nginx / Helm ingress always sits in front of the API; turn off
    # when the API is exposed directly, so a client can't spoof its own IP.
    trust_x_forwarded_for: bool = True
    # Interactive API docs (/docs, /redoc, /openapi.json). Served unauthenticated,
    # so off by default outside development.
    enable_api_docs: bool | None = None
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt credentials in the
    # state file. If empty, one is generated next to the state file on first start
    # (fine for a single container; set it explicitly -- e.g. from a Kubernetes
    # Secret -- anywhere the state volume might be copied or backed up).
    state_encryption_key: str = ""
    # Admin login for the UI. On first visit the UI asks the administrator to
    # set a password (stored as a scrypt hash next to the state file); after
    # that every visit shows a login page and the API accepts the resulting
    # session token as a bearer token alongside API_AUTH_TOKEN (which keeps
    # working for scripts and, when set, is also required as a bootstrap token
    # to set the first password -- so nobody else can claim an exposed
    # instance before you do). Set to false to go back to token-only/open mode.
    admin_login_enabled: bool = True
    admin_password_min_length: int = 12
    # Browser sessions: idle timeout (sliding) and absolute lifetime. In-memory
    # and per-process, like the throttle -- a backend restart logs everyone out.
    session_idle_timeout_s: int = 12 * 60 * 60
    session_max_age_s: int = 7 * 24 * 60 * 60

    # --- Destination: Couchbase Server / Capella ---
    # Same supported range as the sibling couchbase-migration-agent project, since
    # the destination side here is always Couchbase Server/Capella too.
    min_supported_cb_version: str = "7.2.0"
    max_supported_cb_version: str = "8.0.2"

    # --- Agent memory: Couchbase Enterprise Edition (free for dev/test) ---
    memory_cb_connection_string: str = "couchbase://couchbase-memory"
    memory_cb_username: str = "Administrator"
    memory_cb_password: str = "password"
    memory_cb_bucket: str = "agent_memory"
    memory_cb_scope: str = "agent"
    memory_cb_collection: str = "episodes"
    memory_cb_vector_index: str = "agent_memory_vector_idx"
    # Must match the actual output length of QWEN_EMBEDDING_MODEL_NAME's /api/embeddings
    # response (768 for nomic-embed-text, 1024 for qwen3-embedding:0.6b, 2560 for
    # qwen3:4b, 4096 for qwen3:8b). scripts/init_memory.py writes it into the FTS
    # vector index and recreates the index when it changes; existing memories keep
    # their old vectors and simply stop matching until re-embedded (see README).
    memory_embedding_dims: int = 768

    # --- Local LLM, served via an Ollama-compatible API ---
    # The assistant only explains supplied context (validation results, stats,
    # recalled events); nothing on the migration's critical path calls it, so a
    # 4B model is the smallest that stays useful (qwen3:1.7b runs but explains
    # less; qwen3:8b is the previous default if you have the RAM). Embeddings use
    # a dedicated small model -- better recall than a generative model's hidden
    # state, at a fraction of the size.
    # Off: no chat assistant and no memory embeddings (events are still recorded
    # and listed, just not matched by similarity); nothing else changes. Compose
    # doesn't build or start the Ollama container (COMPOSE_PROFILES); Helm skips
    # the LLM Deployment (llm.enabled=false).
    llm_enabled: bool = True
    qwen_base_url: str = "http://qwen-service:11434"
    qwen_model_name: str = "qwen3:4b"
    qwen_embedding_model_name: str = "nomic-embed-text"
    qwen_request_timeout_s: int = 120

    # --- Bring your own model (optional; see core/llm_settings.py) ---
    # Environment defaults for the assistant's provider. Whatever an admin saves
    # on the UI's Settings page (or at first-run setup) takes precedence over
    # these. `local` keeps the bundled Ollama service above; the others send
    # prompts (validation messages, migration stats, the user's questions --
    # never database credentials) to that provider.
    # LLM_PROVIDER: local | openai | openai_compatible | azure_openai | anthropic
    llm_provider: str = "local"
    llm_model: str = ""              # chat model (Azure: deployment name)
    llm_base_url: str = ""           # required for openai_compatible / azure_openai
    llm_api_key: str = ""
    llm_azure_api_version: str = "2024-10-21"
    # local | provider | none -- where agent-memory embeddings come from when a
    # hosted provider is selected (MEMORY_EMBEDDING_DIMS must match the model).
    llm_embedding_source: str = "local"
    llm_embedding_model: str = ""

    # --- Capella Management API (destination auto-provisioning) ---
    capella_api_base_url: str = "https://cloudapi.cloud.couchbase.com/v4"
    capella_api_token: str = ""
    capella_org_id: str = ""

    # --- Migration engine ---
    default_concurrency: int = 8
    default_batch_size: int = 500
    migration_state_file: str = "/data/state/migrations.json"
    # Directory extractors may use for source-native tooling that needs a working
    # dir (e.g. mongoexport/mongodump temp files, DSBulk unload staging). Not a
    # durable backup archive the way cbbackupmgr's is -- see ARCHITECTURE.md /
    # README.md "Why there's no separate backup step" for the rationale.
    scratch_dir: str = "/data/scratch"

    # --- Source: MongoDB ---
    mongodb_batch_size: int = 1000

    # --- Source: Amazon DynamoDB ---
    dynamodb_scan_page_size: int = 1000

    # --- Source: Redis ---
    redis_scan_count: int = 1000

    # --- Source: Apache Cassandra ---
    cassandra_fetch_size: int = 2000
    # Polling interval for Cassandra's polling-based CDC (see
    # core/connectors/cassandra_connector.py -- Cassandra has no CDC mechanism this
    # app can consume without extra infra, so incremental sync polls instead).
    cassandra_cdc_poll_interval_s: int = 30

    # --- Source: Azure Cosmos DB ---
    cosmosdb_page_size: int = 1000
    cosmosdb_change_feed_poll_interval_s: int = 5

    # --- Source: Couchbase Server (CE/EE) / Capella -- native tooling ---
    # A deliberate exception to this app's usual architecture: every other source
    # is strictly read-only, moved through a generic per-document extract/upsert
    # pipeline (see README's "Why there's no separate backup step"). A Couchbase
    # source instead uses Couchbase's own native tools, same as the sibling
    # couchbase-migration-agent project -- cbbackupmgr for one-time/full-load
    # transfer, XDCR for continuous replication -- since they're the correct,
    # battle-tested tools for a same-product migration. See
    # core/couchbase_native.py's module docstring for what that trades away.
    cbbackupmgr_path: str = "cbbackupmgr"
    couchbase_backup_archive_dir: str = "/data/cbbackupmgr-archives"
    xdcr_poll_interval_s: int = 5
    # Community Edition / Capella sources have no XDCR here, so continuous sync
    # polls N1QL for documents whose META().cas moved past a watermark. The
    # overlap re-reads a few seconds of changes each poll to absorb clock skew
    # between nodes (re-applying a document is idempotent).
    couchbase_cdc_poll_interval_s: int = 5
    couchbase_cdc_overlap_s: int = 10
    couchbase_cdc_page_size: int = 1000

    # --- Continuous sync (all sources) ---
    cdc_batch_size: int = 200              # change events applied per micro-batch
    cdc_batch_max_wait_ms: int = 500       # flush a partial batch after this long
    cdc_write_retries: int = 3             # per event, before it's recorded as failed
    cdc_max_consecutive_failures: int = 20  # stream restarts before the migration fails
    cdc_retry_max_backoff_s: int = 300
    dynamodb_shard_discovery_interval_s: int = 30
    # Redis keyspace notifications aren't durable, so during a bulk copy the
    # connector remembers which keys changed and re-reads them afterwards. Past
    # this many distinct keys it falls back to a full re-scan instead.
    redis_dirty_key_limit: int = 1_000_000

    # Reconciliation refuses to delete more than this fraction of a collection's
    # migrated documents in one run without an explicit force from the UI -- a
    # guard against a source listing that silently came back short.
    reconcile_max_delete_fraction: float = 0.2
    # Resume continuous sync / fallback sync automatically when the backend
    # restarts (from the last checkpoint), instead of marking them failed.
    auto_resume_on_startup: bool = True

    # Cutover waits this long for the change stream to report it's caught up
    # before verifying anyway (the log says so if it had to).
    cutover_drain_timeout_s: int = 120
    # Count keys at the source during verification (keys only; a full key scan
    # per container). Disable for very large sources where the destination
    # count alone is enough.
    verify_source_counts: bool = True

    # --- Post-cutover fallback (Couchbase -> source reverse sync) ---
    fallback_poll_interval_s: int = 5
    fallback_delete_check_interval_s: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
