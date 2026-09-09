"""Central configuration, loaded from environment variables (see env.example)."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- App ---
    app_name: str = "Couchbase Onboarding Agent"
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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
    # response -- see couchbase-memory/vector_index.json, which must stay in sync.
    memory_embedding_dims: int = 4096

    # --- Local LLM: Qwen 3, 8B params, served via Ollama-compatible API ---
    qwen_base_url: str = "http://qwen-service:11434"
    qwen_model_name: str = "qwen3:8b"
    qwen_embedding_model_name: str = "qwen3:8b"
    qwen_request_timeout_s: int = 120

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
