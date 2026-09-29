"""Shared enumerations used across the onboarding agent."""
from enum import Enum


class SourceType(str, Enum):
    MONGODB = "mongodb"
    DYNAMODB = "dynamodb"
    REDIS = "redis"
    CASSANDRA = "cassandra"
    COSMOSDB = "cosmosdb"
    COUCHBASE = "couchbase"                    # Community Edition, self-managed
    COUCHBASE_ENTERPRISE = "couchbase_enterprise"  # Enterprise Edition, self-managed
    COUCHBASE_CAPELLA = "couchbase_capella"


# All three source flavors of the same product -- migration_engine.py routes
# these to the native cbbackupmgr/XDCR pipeline (see couchbase_native.py)
# instead of the generic per-document extract/upsert pipeline every other
# source uses.
COUCHBASE_SOURCE_TYPES = {SourceType.COUCHBASE, SourceType.COUCHBASE_ENTERPRISE, SourceType.COUCHBASE_CAPELLA}


class MigrationStrategy(str, Enum):
    """How data moves from source to Couchbase, chosen in the wizard's "Destination
    & Mode" step:
      - FULL_LOAD      -> "One-time migration": every container is fully extracted
        and loaded into Couchbase once. Terminal at COMPLETE; nothing keeps syncing.
      - CDC_LIVE       -> "Continuous replication": no bulk load step -- the
        connector's change-capture mechanism (Mongo change streams, DynamoDB
        Streams, Cosmos DB change feed, Redis keyspace notifications, or Cassandra
        polling) is started immediately and left running. Sits in REPLICATING until
        the user stops it or performs a cutover.
      - FULL_LOAD_AND_CDC -> "Bulk copy + continuous sync": a full load moves
        existing data, then the same change-capture mechanism takes over for the
        ongoing delta, landing in REPLICATING same as CDC_LIVE.
    """
    FULL_LOAD = "full_load"
    CDC_LIVE = "cdc_live"
    FULL_LOAD_AND_CDC = "full_load_and_cdc"


CONTINUOUS_STRATEGIES = {MigrationStrategy.CDC_LIVE, MigrationStrategy.FULL_LOAD_AND_CDC}


class MigrationPhase(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    MIGRATING = "migrating"          # initial full load actively running
    REPLICATING = "replicating"      # continuous change-data-capture sync is live
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    STOPPED = "stopped"              # continuous replication stopped without cutover
    CANCELLED = "cancelled"
    # Post-cutover fallback: Couchbase is the system of record, and changes made
    # there are being replicated back to the source so applications can fall
    # back to it without losing writes (see core/fallback.py).
    FALLBACK_SYNCING = "fallback_syncing"
    FELL_BACK = "fell_back"          # applications moved back to the source


class SyncStage(str, Enum):
    """Where a continuous (CDC_LIVE / FULL_LOAD_AND_CDC) migration is inside its
    replication lifecycle. Reported on MigrationRecord.sync so the UI can show
    whether the destination is still catching up or tracking the source live."""
    IDLE = "idle"
    CAPTURING = "capturing"          # change-capture start position recorded, before bulk copy
    BULK_COPY = "bulk_copy"          # full load running while changes queue at the source
    CATCH_UP = "catch_up"            # applying changes that happened during the bulk copy
    STREAMING = "streaming"          # caught up; applying changes as they happen
    DRAINING = "draining"            # cutover requested; applying what's left before verifying
    RECOVERING = "recovering"        # change stream dropped; retrying from the last checkpoint
    STOPPED = "stopped"


class CdcMethod(str, Enum):
    """How a connector captures ongoing changes -- surfaced in the UI so operators
    know which guarantees they get (resumable, sees deletes) per source."""
    CHANGE_STREAM = "change_stream"          # MongoDB change streams
    STREAM_SHARDS = "stream_shards"          # DynamoDB Streams
    CHANGE_FEED = "change_feed"              # Cosmos DB change feed
    WRITETIME_POLL = "writetime_poll"        # Cassandra WRITETIME() polling
    KEYSPACE_EVENTS = "keyspace_events"      # Redis keyspace notifications
    XDCR = "xdcr"                            # Couchbase EE -> Couchbase
    CAS_POLL = "cas_poll"                    # Couchbase CE / Capella, via N1QL on META().cas


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationCheckId(str, Enum):
    SOURCE_CONNECTIVITY = "source_connectivity"
    SOURCE_EDITION = "source_edition"
    DEST_CONNECTIVITY = "dest_connectivity"
    DEST_CAPACITY = "dest_capacity"
    CDC_SUPPORT = "cdc_support"
    SCHEMA_INFERENCE = "schema_inference"
    DOCUMENT_SIZE_LIMIT = "document_size_limit"
    NETWORK_LATENCY = "network_latency"
    TLS_CONFIG = "tls_config"
    NAMING_COMPAT = "naming_compat"
    XDCR_VBUCKET_COMPAT = "xdcr_vbucket_compat"
    TLS_VERIFICATION = "tls_verification"
    DELETE_PROPAGATION = "delete_propagation"
    ROLLBACK_SAFETY = "rollback_safety"


class BottleneckKind(str, Enum):
    """Categories of ETL bottleneck the agent watches for while a migration is
    actively running. Unlike the sibling couchbase-migration-agent project (which
    watches cbbackupmgr, a subprocess it launches), this app's extract/load pipeline
    is a pool of asyncio worker tasks it fully owns -- see core/bottleneck_detector.py.
    """
    THROUGHPUT_STALLED = "throughput_stalled"        # ~0 docs/s for a sustained window
    THROUGHPUT_DEGRADED = "throughput_degraded"       # well below this run's own peak
    SOURCE_THROTTLED = "source_throttled"             # source reported rate-limit errors
    DEST_BACKPRESSURE = "dest_backpressure"           # Couchbase temp failures / durability timeouts
    CONCURRENCY_REDUCED = "concurrency_reduced"        # auto-throttle already acted


class BackupStatus(str, Enum):
    """Kept for API/UI symmetry with the sibling project even though this app has no
    separate backup phase (see README "Why there's no separate backup step"). Used
    only for the optional pre-migration source export some connectors can produce."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


MIN_SUPPORTED_CB_VERSION = (7, 2, 0)
MAX_SUPPORTED_CB_VERSION = (8, 0, 2)
