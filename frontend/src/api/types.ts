export type SourceType =
  | "mongodb"
  | "dynamodb"
  | "redis"
  | "cassandra"
  | "cosmosdb"
  | "couchbase"
  | "couchbase_enterprise"
  | "couchbase_capella";

export type MigrationStrategy = "full_load" | "cdc_live" | "full_load_and_cdc";

export type MigrationPhase =
  | "draft"
  | "validating"
  | "validated"
  | "validation_failed"
  | "awaiting_approval"
  | "approved"
  | "migrating"
  | "replicating"
  | "verifying"
  | "complete"
  | "failed"
  | "rolling_back"
  | "rolled_back"
  | "stopped"
  | "cancelled";

export interface SourceConnectionConfig {
  label: string;
  source_type: SourceType;
  connection_string?: string;
  database?: string;
  username?: string;
  password?: string;
  use_tls?: boolean;
  ca_cert_path?: string;
  couchbase_external_network?: boolean;
  redis_db_index?: number;
  cassandra_port?: number;
  cassandra_datacenter?: string;
  aws_region?: string;
  aws_access_key_id?: string;
  aws_secret_access_key?: string;
  aws_session_token?: string;
  dynamodb_endpoint_url?: string;
  cosmos_endpoint?: string;
  cosmos_key?: string;
}

export interface CouchbaseConnectionConfig {
  label: string;
  connection_string: string;
  username: string;
  password: string;
  is_capella?: boolean;
  capella_cluster_id?: string;
  capella_project_id?: string;
  use_tls?: boolean;
  ca_cert_path?: string;
}

export interface SourceContainerStats {
  name: string;
  estimated_count?: number | null;
  estimated_size_bytes?: number | null;
  sample_fields: string[];
  notes: string[];
}

export interface SourceTopologySnapshot {
  source_type: SourceType;
  server_version?: string | null;
  server_edition?: string | null;
  containers: SourceContainerStats[];
  total_estimated_count?: number | null;
  total_estimated_size_bytes?: number | null;
  supports_cdc: boolean;
  cdc_notes?: string | null;
}

export interface CouchbaseTopologySnapshot {
  cluster_uuid?: string | null;
  cluster_version?: string | null;
  nodes: { hostname: string; services: string[]; version?: string | null; status?: string | null }[];
  buckets: string[];
  scopes_by_bucket: Record<string, string[]>;
  collections_by_bucket: Record<string, string[]>;
  total_docs?: number | null;
  total_data_size_bytes?: number | null;
}

export interface ValidationCheckResult {
  check_id: string;
  label: string;
  severity: "info" | "warning" | "error";
  passed: boolean;
  message: string;
  details: Record<string, unknown>;
}

export interface ValidationReport {
  migration_id: string;
  generated_at: string;
  checks: ValidationCheckResult[];
  source_topology?: SourceTopologySnapshot | null;
  dest_topology?: CouchbaseTopologySnapshot | null;
  passed: boolean;
  has_warnings: boolean;
}

export interface ContainerMigrationSpec {
  container_name: string;
  include: boolean;
  target_scope_name?: string | null;
  target_collection_name?: string | null;
}

export interface MigrationPlanCreate {
  name: string;
  source: SourceConnectionConfig;
  destination: CouchbaseConnectionConfig;
  destination_bucket: string;
  destination_bucket_ram_quota_mb: number;
  strategy: MigrationStrategy;
  containers: ContainerMigrationSpec[];
  concurrency: number;
  rate_limit_docs_per_sec?: number | null;
  is_continuous?: boolean;
}

export interface BottleneckFinding {
  finding_id: string;
  kind: string;
  phase: string;
  message: string;
  suggestion: string;
  detected_at: string;
  recommended_concurrency?: number | null;
  auto_remediated: boolean;
}

export interface MigrationStats {
  docs_total: number;
  docs_migrated: number;
  docs_failed: number;
  bytes_migrated: number;
  throughput_docs_per_sec: number;
  avg_latency_ms: number;
  error_rate_pct: number;
  elapsed_seconds: number;
  eta_seconds?: number | null;
  per_container: Record<string, Record<string, number>>;
  replication_active: boolean;
  changes_left?: number | null;
  mutations_replicated: number;
  mutations_per_sec: number;
  replication_lag_seconds?: number | null;
  last_replication_poll?: string | null;
}

export interface MigrationRecord {
  migration_id: string;
  plan: MigrationPlanCreate;
  phase: MigrationPhase;
  created_at: string;
  updated_at: string;
  approved_by?: string | null;
  approved_at?: string | null;
  validation_report?: ValidationReport | null;
  stats: MigrationStats;
  log_tail: string[];
  error_message?: string | null;
  bottleneck_findings: BottleneckFinding[];
  checkpoint: Record<string, unknown>;
}

export interface ReplicationModeRecommendationResponse {
  recommended_strategy: MigrationStrategy;
  headline: string;
  rationale: string;
  considerations: string[];
  estimated_duration_seconds?: number | null;
}

export interface AgentChatResponse {
  reply: string;
  recalled_memories: string[];
  suggested_actions: string[];
}

export type AgentHealthState = "ready" | "waiting" | "error";

export interface AgentStatusResponse {
  status: AgentHealthState;
  detail: string;
}
