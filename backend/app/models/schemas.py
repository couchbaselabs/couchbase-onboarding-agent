"""Pydantic schemas shared across the API, migration engine, connectors, and agent."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field, field_validator

from app.models.enums import (
    BottleneckKind,
    MigrationPhase,
    MigrationStrategy,
    SourceType,
    ValidationCheckId,
    ValidationSeverity,
)

# ---------------------------------------------------------------------------
# Source connection configuration
# ---------------------------------------------------------------------------


class SourceConnectionConfig(BaseModel):
    """User-supplied connection details for a source database. Deliberately one
    flat model covering all five source types rather than a discriminated union --
    the wizard only shows the fields relevant to the selected source_type, and a
    flat model keeps the connector interface (SourceConnector.__init__) uniform.
    See app/core/connectors/base.py for how each connector reads just the fields
    it needs."""

    label: str = Field(..., description="Friendly name shown in the UI")
    source_type: SourceType

    # -- MongoDB / Redis / Cassandra (host-and-port style) --
    connection_string: Optional[str] = Field(
        None,
        description="mongodb://..., mongodb+srv://..., redis://..., or a comma-separated "
        "list of Cassandra contact points (host[:port], ...).",
    )
    database: Optional[str] = Field(
        None, description="MongoDB database name, Cassandra keyspace, or Cosmos DB database name."
    )
    username: Optional[str] = None
    password: Optional[str] = Field(None, repr=False)
    use_tls: bool = False
    ca_cert_path: Optional[str] = None

    # -- Redis specifics --
    redis_db_index: int = 0

    # -- Cassandra specifics --
    cassandra_port: int = 9042
    cassandra_datacenter: Optional[str] = Field(
        None, description="Local datacenter name, required by the driver's default load-balancing policy."
    )

    # -- Amazon DynamoDB (AWS SDK / boto3-based, no host string) --
    aws_region: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = Field(None, repr=False)
    aws_session_token: Optional[str] = Field(None, repr=False)
    dynamodb_endpoint_url: Optional[str] = Field(
        None, description="Override for DynamoDB Local / a VPC endpoint; leave blank for real AWS."
    )

    # -- Azure Cosmos DB (SQL/Core API, via azure-cosmos SDK) --
    cosmos_endpoint: Optional[str] = None
    cosmos_key: Optional[str] = Field(None, repr=False)

    # -- Couchbase source specifics --
    couchbase_external_network: bool = Field(
        False,
        description="Set when the source Couchbase cluster runs on a cloud VM or Kubernetes "
        "(EC2, GKE, etc.): tells the SDK to prefer each node's external/alternate address "
        "(ClusterOptions(network='external')) instead of the internal address the cluster's "
        "config returns after the initial connection -- otherwise KV/N1QL requests to nodes "
        "other than the seed host fail with connection errors once the SDK has the full "
        "cluster map. Requires alternate addressing to already be configured on the source "
        "cluster itself (Couchbase Web Console -> Server Nodes -> External Address).",
    )

    @field_validator("connection_string")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else v


# ---------------------------------------------------------------------------
# Destination (Couchbase Server / Capella) connection configuration
# ---------------------------------------------------------------------------


class CouchbaseConnectionConfig(BaseModel):
    label: str = Field(..., description="Friendly name shown in the UI")
    connection_string: str = Field(
        ..., description="couchbase:// or couchbases:// connection string, or a Capella endpoint."
    )
    username: str
    password: str = Field(..., repr=False)
    is_capella: bool = False
    capella_cluster_id: Optional[str] = None
    capella_project_id: Optional[str] = None
    use_tls: bool = True
    ca_cert_path: Optional[str] = None

    @field_validator("connection_string")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        if not (v.startswith("couchbase://") or v.startswith("couchbases://") or v.startswith("https://")):
            raise ValueError(
                "connection_string must start with couchbase://, couchbases://, or https:// (Capella)"
            )
        return v


class CouchbaseNode(BaseModel):
    hostname: str
    services: list[str] = Field(default_factory=lambda: ["kv"])
    version: Optional[str] = None
    status: Optional[str] = "healthy"


class CouchbaseTopologySnapshot(BaseModel):
    """Introspected topology of the Couchbase destination, populated by the validator."""

    cluster_uuid: Optional[str] = None
    cluster_version: Optional[str] = None
    nodes: list[CouchbaseNode] = Field(default_factory=list)
    buckets: list[str] = Field(default_factory=list)
    scopes_by_bucket: dict[str, list[str]] = Field(default_factory=dict)
    collections_by_bucket: dict[str, list[str]] = Field(default_factory=dict)
    total_docs: Optional[int] = None
    total_data_size_bytes: Optional[int] = None


# ---------------------------------------------------------------------------
# Source topology (introspection)
# ---------------------------------------------------------------------------


class SourceContainerStats(BaseModel):
    """One "container" = a MongoDB collection, a DynamoDB table, a Redis logical
    keyspace, a Cassandra table (keyspace.table), or a Cosmos DB container."""

    name: str
    estimated_count: Optional[int] = None
    estimated_size_bytes: Optional[int] = None
    sample_fields: list[str] = Field(default_factory=list)
    # Best-effort per-source notes surfaced next to the container in the wizard's
    # bucket-mapping-equivalent step, e.g. "no secondary index", "TTL enabled".
    notes: list[str] = Field(default_factory=list)


class SourceTopologySnapshot(BaseModel):
    source_type: SourceType
    server_version: Optional[str] = None
    server_edition: Optional[str] = None  # e.g. "MongoDB 7.0 (replica set)", "Cassandra 4.1"
    containers: list[SourceContainerStats] = Field(default_factory=list)
    total_estimated_count: Optional[int] = None
    total_estimated_size_bytes: Optional[int] = None
    supports_cdc: bool = False
    cdc_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationCheckResult(BaseModel):
    check_id: ValidationCheckId
    label: str
    severity: ValidationSeverity
    passed: bool
    message: str
    details: dict = Field(default_factory=dict)


class ValidationReport(BaseModel):
    migration_id: UUID
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    checks: list[ValidationCheckResult] = Field(default_factory=list)
    source_topology: Optional[SourceTopologySnapshot] = None
    dest_topology: Optional[CouchbaseTopologySnapshot] = None

    # Must be @computed_field, not a plain @property -- pydantic v2 only serializes
    # plain @property on request (excluded from model_dump()/JSON by default), which
    # would otherwise make the frontend see `validation.passed === undefined`.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == ValidationSeverity.ERROR)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_warnings(self) -> bool:
        return any(not c.passed and c.severity == ValidationSeverity.WARNING for c in self.checks)


# ---------------------------------------------------------------------------
# Migration plan / status
# ---------------------------------------------------------------------------


class ContainerMigrationSpec(BaseModel):
    container_name: str
    include: bool = True
    # Defaults, applied by the migration engine if left blank: target_scope_name
    # falls back to a sanitized version of the source database/keyspace name (or
    # "_default"); target_collection_name falls back to a sanitized container name.
    target_scope_name: Optional[str] = None
    target_collection_name: Optional[str] = None


class MigrationPlanCreate(BaseModel):
    name: str
    source: SourceConnectionConfig
    destination: CouchbaseConnectionConfig
    destination_bucket: str = Field(..., description="Couchbase bucket documents are written into.")
    destination_bucket_ram_quota_mb: int = 1024
    strategy: MigrationStrategy = MigrationStrategy.FULL_LOAD
    containers: list[ContainerMigrationSpec] = Field(default_factory=list)
    concurrency: int = Field(8, ge=1, le=64)
    rate_limit_docs_per_sec: Optional[int] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_continuous(self) -> bool:
        """True for replication modes that stay running (CDC_LIVE / FULL_LOAD_AND_CDC)
        rather than completing after a single pass (FULL_LOAD)."""
        return self.strategy in (MigrationStrategy.CDC_LIVE, MigrationStrategy.FULL_LOAD_AND_CDC)


class MigrationStats(BaseModel):
    docs_total: int = 0
    docs_migrated: int = 0
    docs_failed: int = 0
    bytes_migrated: int = 0
    throughput_docs_per_sec: float = 0.0
    avg_latency_ms: float = 0.0
    error_rate_pct: float = 0.0
    elapsed_seconds: float = 0.0
    eta_seconds: Optional[float] = None
    per_container: dict[str, dict] = Field(default_factory=dict)

    # -- continuous replication (CDC_LIVE / FULL_LOAD_AND_CDC) only --
    replication_active: bool = False
    changes_left: Optional[int] = None
    mutations_replicated: int = 0
    mutations_per_sec: float = 0.0
    replication_lag_seconds: Optional[float] = None
    last_replication_poll: Optional[datetime] = None


class BottleneckFinding(BaseModel):
    """A single detected pipeline bottleneck, produced by BottleneckMonitor
    (app/core/bottleneck_detector.py) while a migration is actively running. Unlike
    the sibling couchbase-migration-agent project (which watches a cbbackupmgr
    subprocess), this pipeline is an asyncio worker pool this app owns outright, so
    both throughput-trend AND resource-pressure findings can be auto-remediated by
    adjusting `concurrency` -- see MigrationEngine._run_pipeline()'s auto-throttle
    loop. auto_remediated is True on the follow-up finding describing what was done."""

    finding_id: UUID = Field(default_factory=uuid4)
    kind: BottleneckKind
    phase: str  # "initial_load" or "replication"
    message: str
    suggestion: str
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    recommended_concurrency: Optional[int] = None
    auto_remediated: bool = False


class MigrationRecord(BaseModel):
    migration_id: UUID = Field(default_factory=uuid4)
    plan: MigrationPlanCreate
    phase: MigrationPhase = MigrationPhase.DRAFT
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    validation_report: Optional[ValidationReport] = None
    stats: MigrationStats = Field(default_factory=MigrationStats)
    log_tail: list[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    bottleneck_findings: list[BottleneckFinding] = Field(default_factory=list)
    # Opaque, connector-defined resume tokens keyed by container name -- e.g. a Mongo
    # change-stream resume token, a DynamoDB Streams shard iterator/sequence number,
    # or a Cosmos DB change-feed continuation token. Persisted so a restarted backend
    # can resume CDC without re-reading from the beginning. See
    # app/core/connectors/base.py's stream_changes() contract.
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class MigrationApproval(BaseModel):
    migration_id: UUID
    approved_by: str
    confirm_source_reviewed: bool = True


class RollbackRequest(BaseModel):
    migration_id: UUID
    reason: str = "user_requested"
    # This app never writes to the source -- every connector is read-only (see
    # README "Why there's no separate backup step") -- so rollback means undoing
    # the *destination* side: stop any active CDC and, if requested, delete every
    # document this migration wrote to Couchbase (tagged via each doc's
    # `_migration.migration_id` field -- see couchbase_loader.py).
    purge_destination_data: bool = True


class ReplicationStopRequest(BaseModel):
    migration_id: UUID
    perform_cutover: bool = True


# ---------------------------------------------------------------------------
# Agent chat / memory
# ---------------------------------------------------------------------------


class AgentChatMessage(BaseModel):
    role: str  # "user" | "assistant" | "system"
    content: str
    migration_id: Optional[UUID] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentChatRequest(BaseModel):
    migration_id: Optional[UUID] = None
    message: str
    use_memory: bool = True


class AgentChatResponse(BaseModel):
    reply: str
    recalled_memories: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)


class AgentStatusResponse(BaseModel):
    """Reachability of the local Qwen LLM the chat/reasoning agent runs on --
    polled by the sidebar status indicator. 'waiting' specifically means the
    server is up but the model is still being pulled (first-boot only)."""

    status: str  # "ready" | "waiting" | "error"
    detail: str


# ---------------------------------------------------------------------------
# Replication mode recommendation (Destination & Mode wizard step)
# ---------------------------------------------------------------------------


class ReplicationModeRecommendationRequest(BaseModel):
    cutover_plan: str = Field(..., description="'cutover' (all applications switch over at once) or 'phased'")
    source_topology: SourceTopologySnapshot
    concurrency: int = Field(8, ge=1, le=64)


class ReplicationModeRecommendationResponse(BaseModel):
    recommended_strategy: MigrationStrategy
    headline: str
    rationale: str
    considerations: list[str] = Field(default_factory=list)
    estimated_duration_seconds: Optional[float] = None
