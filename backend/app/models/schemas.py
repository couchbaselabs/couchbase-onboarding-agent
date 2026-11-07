"""Pydantic schemas shared across the API, migration engine, connectors, and agent."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field, field_validator

from app.models.enums import (
    BottleneckKind,
    CdcMethod,
    MigrationPhase,
    MigrationStrategy,
    SourceType,
    SyncStage,
    ValidationCheckId,
    ValidationSeverity,
)

# Couchbase's own rule for bucket names (letters, digits, `.`, `_`, `%`, `-`; at
# most 100 characters). Enforced on every bucket name an operator types so a
# name can never carry N1QL syntax into the backtick-quoted keyspaces the
# loader builds (`bucket`.`scope`.`collection`).
BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9._%-]{1,100}$")


def validate_bucket_name(name: str) -> str:
    name = name.strip()
    if not BUCKET_NAME_RE.match(name):
        raise ValueError(
            "bucket names may only contain letters, digits, '.', '_', '%' and '-' (1-100 characters)"
        )
    return name


# Every connection field that holds a credential. These are masked in every API
# response and websocket frame (see redact_record) and encrypted at rest in the
# state file (see core/store.py), so a GET /api/migrations never hands source or
# destination passwords back to a browser.
SECRET_FIELDS: frozenset[str] = frozenset({
    "password", "aws_secret_access_key", "aws_session_token", "cosmos_key", "ca_cert_pem",
})
REDACTED = "********"

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
    ca_cert_pem: Optional[str] = Field(
        None, repr=False,
        description="PEM-encoded CA certificate to trust for this source, pasted in the wizard. "
        "Takes precedence over ca_cert_path (which must be a path inside the backend container).",
    )
    tls_skip_verify: bool = Field(
        False,
        description="Disable TLS certificate verification for this source. Only for test clusters "
        "with self-signed certificates -- the validator flags it as a warning.",
    )

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
    ca_cert_pem: Optional[str] = Field(None, repr=False)
    tls_skip_verify: bool = Field(
        False,
        description="Disable TLS certificate verification for management REST calls and "
        "cbbackupmgr. Capella's management endpoint presents a Couchbase-issued CA; paste that "
        "CA into ca_cert_pem instead of skipping verification where possible.",
    )

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
    # What continuous sync gives you on this source, so the wizard can say it
    # plainly instead of burying it in cdc_notes.
    cdc_method: Optional[CdcMethod] = None
    cdc_resumable: bool = False          # survives a backend restart without a re-scan
    cdc_captures_deletes: bool = False   # deletes flow through CDC (otherwise via reconciliation)
    reverse_sync_supported: bool = False  # post-cutover fallback can write back to this source


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


class TargetKeyspace(BaseModel):
    """One destination keyspace a source container is written to. Any part left
    blank falls back to the plan's defaults: `bucket` -> the plan's
    destination_bucket, `scope` -> a sanitized source database/keyspace name (or
    `_default`), `collection` -> a sanitized container name. See
    core/targets.py, which is the single place these defaults are resolved."""

    bucket: Optional[str] = None
    scope: Optional[str] = None
    collection: Optional[str] = None

    @field_validator("bucket", "scope", "collection")
    @classmethod
    def _blank_to_none(cls, v: Optional[str]) -> Optional[str]:
        v = v.strip() if isinstance(v, str) else v
        return v or None

    @field_validator("bucket")
    @classmethod
    def _bucket_name(cls, v: Optional[str]) -> Optional[str]:
        return validate_bucket_name(v) if v else v

    @field_validator("scope", "collection")
    @classmethod
    def _keyspace_part(cls, v: Optional[str]) -> Optional[str]:
        if v and (len(v) > 251 or "`" in v):
            raise ValueError("scope and collection names must be at most 251 characters and contain no backtick")
        return v


class ContainerMigrationSpec(BaseModel):
    container_name: str
    include: bool = True
    # Legacy single-target fields (kept so plans created by earlier versions
    # keep working): applied when `targets` is empty. New plans use `targets`.
    target_scope_name: Optional[str] = None
    target_collection_name: Optional[str] = None
    # Where this container's documents land. One entry is the common case; more
    # than one fans every document out to each keyspace (e.g. a hot copy in a
    # second bucket, or the same data in two scopes for two applications).
    # Verification, reconciliation and fallback treat the FIRST entry as the
    # primary copy; the others receive the same writes. Empty -> one target
    # resolved from the legacy fields / plan defaults.
    targets: list[TargetKeyspace] = Field(default_factory=list)

    def resolved_targets(self) -> list[TargetKeyspace]:
        """`targets`, or the legacy single target when it's empty. Never empty."""
        if self.targets:
            return self.targets
        return [TargetKeyspace(scope=self.target_scope_name, collection=self.target_collection_name)]


class MigrationPlanCreate(BaseModel):
    name: str
    source: SourceConnectionConfig
    destination: CouchbaseConnectionConfig
    destination_bucket: str = Field(
        ..., description="Default Couchbase bucket documents are written into. A container's targets can "
        "name other buckets; every bucket named anywhere in the plan is provisioned if missing.",
    )
    destination_bucket_ram_quota_mb: int = 1024
    strategy: MigrationStrategy = MigrationStrategy.FULL_LOAD

    @field_validator("destination_bucket")
    @classmethod
    def _destination_bucket_name(cls, v: str) -> str:
        return validate_bucket_name(v)
    containers: list[ContainerMigrationSpec] = Field(default_factory=list)
    concurrency: int = Field(8, ge=1, le=64)
    rate_limit_docs_per_sec: Optional[int] = None
    # -- continuous sync / rollback options --
    reconcile_interval_minutes: int = Field(
        60, ge=0, le=7 * 24 * 60,
        description="How often a running continuous sync compares source and destination keys "
        "to remove documents deleted at the source (0 disables). This is how deletes reach "
        "Couchbase for sources whose change capture can't see them.",
    )
    journal_overwrites: bool = Field(
        True,
        description="Before overwriting or deleting a document that already existed in a "
        "destination collection this migration didn't create, save its prior version so "
        "rollback can restore it.",
    )
    create_destination_indexes: bool = Field(
        True,
        description="Create the small GSI indexes verification, rollback and fallback sync "
        "query with (on _migration.migration_id and META().cas).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_continuous(self) -> bool:
        """True for replication modes that stay running (CDC_LIVE / FULL_LOAD_AND_CDC)
        rather than completing after a single pass (FULL_LOAD)."""
        return self.strategy in (MigrationStrategy.CDC_LIVE, MigrationStrategy.FULL_LOAD_AND_CDC)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_buckets(self) -> list[str]:
        """Every destination bucket this plan writes to (the default first)."""
        buckets = [self.destination_bucket]
        for spec in self.containers:
            if not spec.include:
                continue
            for t in spec.resolved_targets():
                if t.bucket and t.bucket not in buckets:
                    buckets.append(t.bucket)
        return buckets


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


class ReconcileStatus(BaseModel):
    running: bool = False
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_result: Optional[str] = None
    deletes_applied_total: int = 0
    missing_in_destination: int = 0  # last run: source keys not found in Couchbase


class FailedEvent(BaseModel):
    """A change event that still failed after retries. Recorded (most recent 100)
    rather than blocking the stream; re-running reconciliation or a bulk copy of
    that container repairs it."""
    container: str
    key: Optional[str] = None
    op: str
    error: str
    at: datetime = Field(default_factory=datetime.utcnow)


class SyncState(BaseModel):
    stage: SyncStage = SyncStage.IDLE
    capture_started_at: Optional[datetime] = None
    bulk_copy_started_at: Optional[datetime] = None
    bulk_copy_finished_at: Optional[datetime] = None
    caught_up_at: Optional[datetime] = None
    drain_requested_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    last_checkpoint_at: Optional[datetime] = None
    stream_restarts: int = 0
    last_stream_error: Optional[str] = None
    cdc_method: Optional[CdcMethod] = None
    reconcile: ReconcileStatus = Field(default_factory=ReconcileStatus)


class RollbackState(BaseModel):
    """What rollback needs to undo this migration's effect on the destination.
    Captured before the first write, so it's accurate even after a crash.

    Keyspaces are "bucket.scope.collection". Records written by earlier versions
    hold "scope.collection" (always in the plan's destination_bucket);
    core/targets.qualify_keyspace() upgrades those on read."""
    captured: bool = False
    bucket_created_by_migration: bool = False
    buckets_created_by_migration: list[str] = Field(default_factory=list)
    preexisting_collections: list[str] = Field(default_factory=list)  # "bucket.scope.collection"
    owned_collections: list[str] = Field(default_factory=list)        # created by this migration
    journal_keyspace: Optional[str] = None                             # "bucket.scope.collection"
    journaled_docs: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    last_summary: Optional[str] = None


class FallbackState(BaseModel):
    """Post-cutover reverse sync (Couchbase -> source). See core/fallback.py."""
    active: bool = False
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    # How changes travel back: "cas_poll" (N1QL on META().cas, batched through
    # the connector's apply_reverse_batch) or "xdcr" (Couchbase EE source with a
    # self-managed destination: a reverse XDCR replication, destination -> source).
    method: Optional[str] = None
    watermarks: dict[str, int] = Field(default_factory=dict)  # "bucket.scope.collection" -> CAS
    changes_applied: int = 0
    deletes_applied: int = 0
    errors: int = 0
    last_poll_at: Optional[datetime] = None
    last_error: Optional[str] = None
    lag_seconds: Optional[float] = None


class ContainerVerification(BaseModel):
    """Per-container, per-target count comparison produced at the end of a
    one-time load and at cutover. source_count is None when the source wasn't
    counted. A container with several targets produces one row per target."""
    container: str
    target: str  # bucket.scope.collection
    source_count: Optional[int] = None
    dest_count: Optional[int] = None
    matched: Optional[bool] = None
    note: Optional[str] = None


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
    sync: SyncState = Field(default_factory=SyncState)
    rollback_state: RollbackState = Field(default_factory=RollbackState)
    fallback: FallbackState = Field(default_factory=FallbackState)
    failed_events: list[FailedEvent] = Field(default_factory=list)
    cutover_at: Optional[datetime] = None
    # Every cutover this migration has been through. Bi-directional operation
    # (cut over -> fall back -> return to Couchbase -> cut over again ...) clears
    # cutover_at on each return; this keeps the history.
    cutover_history: list[datetime] = Field(default_factory=list)
    failbacks: int = 0  # times forward sync was resumed after falling back to the source
    verification: list[ContainerVerification] = Field(default_factory=list)
    verified_at: Optional[datetime] = None


class MigrationApproval(BaseModel):
    migration_id: UUID
    approved_by: str
    confirm_source_reviewed: bool = True


class RollbackRequest(BaseModel):
    migration_id: UUID
    reason: str = "user_requested"
    # Forward migration never writes to the source, so rollback undoes the
    # *destination* side: stop any active sync, drop collections this migration
    # created, remove documents it wrote into pre-existing collections, and
    # restore the documents it overwrote there from the pre-image journal.
    purge_destination_data: bool = True
    drop_owned_collections: bool = True
    restore_overwritten: bool = True
    # Rolling back after cutover deletes data applications may have written to
    # Couchbase since. Require an explicit opt-in (the UI asks for the
    # migration name); a post-cutover fallback sync is the safer path.
    confirm_after_cutover: bool = False
    # Drop the destination bucket if this migration created it (self-managed
    # clusters only; a Capella bucket is dropped from the Capella UI).
    drop_created_bucket: bool = False


class RollbackPlan(BaseModel):
    """Dry run of what POST /rollback would do -- shown before the user confirms."""
    migration_id: UUID
    phase: MigrationPhase
    allowed: bool
    requires_cutover_confirmation: bool
    steps: list[str]
    warnings: list[str] = Field(default_factory=list)


class ReconcileRequest(BaseModel):
    migration_id: UUID
    # Apply deletes even past RECONCILE_MAX_DELETE_FRACTION (the operator
    # confirmed that a large deletion at the source is expected).
    force: bool = False


class FallbackStopRequest(BaseModel):
    migration_id: UUID
    # True: final drain, then mark FELL_BACK (the source is the system of record
    # again). False: stop reverse sync and stay on Couchbase (COMPLETE).
    fall_back_to_source: bool = False


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


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


def _mask_secrets(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            k: (REDACTED if k in SECRET_FIELDS and v else _mask_secrets(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_mask_secrets(v) for v in data]
    return data


def redact_record(record: MigrationRecord) -> dict[str, Any]:
    """JSON-ready copy of a record with every credential masked. Use this for
    anything that leaves the backend (REST responses, websocket frames, logs)."""
    return _mask_secrets(record.model_dump(mode="json"))
