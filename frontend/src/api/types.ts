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
  | "cancelled"
  | "fallback_syncing"
  | "fell_back";

export type SyncStage =
  | "idle"
  | "capturing"
  | "bulk_copy"
  | "catch_up"
  | "streaming"
  | "recovering"
  | "draining"
  | "stopped";

export type CdcMethod =
  | "change_stream"
  | "stream_shards"
  | "change_feed"
  | "writetime_poll"
  | "keyspace_events"
  | "xdcr"
  | "cas_poll";

export interface SourceTypeInfo {
  value: SourceType;
  label: string;
  fields: string[];
  cdc: { method: CdcMethod | null; resumable: boolean; captures_deletes: boolean; reverse_sync: boolean };
}

export interface SourceConnectionConfig {
  label: string;
  source_type: SourceType;
  connection_string?: string;
  database?: string;
  username?: string;
  password?: string;
  use_tls?: boolean;
  ca_cert_path?: string;
  ca_cert_pem?: string;
  tls_skip_verify?: boolean;
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
  ca_cert_pem?: string;
  tls_skip_verify?: boolean;
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
  cdc_method?: CdcMethod | null;
  cdc_resumable?: boolean;
  cdc_captures_deletes?: boolean;
  reverse_sync_supported?: boolean;
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

/** One destination keyspace. Blank parts fall back to the plan's defaults
 * (destination bucket; a scope named after the source database or `_default`;
 * a collection named after the container). */
export interface TargetKeyspace {
  bucket?: string | null;
  scope?: string | null;
  collection?: string | null;
}

export interface ContainerMigrationSpec {
  container_name: string;
  include: boolean;
  /** Legacy single-target fields; ignored when `targets` is non-empty. */
  target_scope_name?: string | null;
  target_collection_name?: string | null;
  /** One or more keyspaces every document of this container is written to.
   * The first is the primary copy (counts, reconciliation, fallback). */
  targets: TargetKeyspace[];
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
  reconcile_interval_minutes?: number;
  journal_overwrites?: boolean;
  create_destination_indexes?: boolean;
  is_continuous?: boolean;
  target_buckets?: string[];
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
  sync: SyncState;
  rollback_state: RollbackState;
  fallback: FallbackState;
  cutover_history?: string[];
  failbacks?: number;
  failed_events: FailedEvent[];
  cutover_at?: string | null;
  verification: ContainerVerification[];
  verified_at?: string | null;
}

export interface ReconcileStatus {
  running: boolean;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_result?: string | null;
  deletes_applied_total: number;
  missing_in_destination: number;
}

export interface SyncState {
  stage: SyncStage;
  capture_started_at?: string | null;
  bulk_copy_started_at?: string | null;
  bulk_copy_finished_at?: string | null;
  caught_up_at?: string | null;
  drain_requested_at?: string | null;
  last_event_at?: string | null;
  last_checkpoint_at?: string | null;
  stream_restarts: number;
  last_stream_error?: string | null;
  cdc_method?: CdcMethod | null;
  reconcile: ReconcileStatus;
}

export interface RollbackState {
  captured: boolean;
  bucket_created_by_migration: boolean;
  buckets_created_by_migration?: string[];
  preexisting_collections: string[];
  owned_collections: string[];
  journal_keyspace?: string | null;
  journaled_docs: number;
  completed_steps: string[];
  last_summary?: string | null;
}

export interface FallbackState {
  active: boolean;
  started_at?: string | null;
  stopped_at?: string | null;
  /** "cas_poll" (N1QL polling + the source's bulk write API) or "xdcr" (reverse XDCR). */
  method?: string | null;
  changes_applied: number;
  deletes_applied: number;
  errors: number;
  last_poll_at?: string | null;
  last_error?: string | null;
  lag_seconds?: number | null;
}

export interface FailedEvent {
  container: string;
  key?: string | null;
  op: string;
  error: string;
  at: string;
}

export interface ContainerVerification {
  container: string;
  target: string;
  source_count?: number | null;
  dest_count?: number | null;
  matched?: boolean | null;
  note?: string | null;
}

export interface RollbackOptions {
  reason?: string;
  purge_destination_data?: boolean;
  drop_owned_collections?: boolean;
  restore_overwritten?: boolean;
  drop_created_bucket?: boolean;
  confirm_after_cutover?: boolean;
}

export interface RollbackPlan {
  migration_id: string;
  phase: MigrationPhase;
  allowed: boolean;
  requires_cutover_confirmation: boolean;
  steps: string[];
  warnings: string[];
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

export type AgentHealthState = "ready" | "waiting" | "error" | "disabled";

export interface AgentStatusResponse {
  status: AgentHealthState;
  detail: string;
  /** Which model backs the assistant (core/llm_settings.py). */
  provider?: LlmProvider;
  provider_label?: string;
  model?: string;
  /** false when prompts leave this host (a hosted provider is configured). */
  local?: boolean;
  /** Local provider only, once ready: where Ollama has the model loaded
   * (from /api/ps). "unknown" until the first request loads it. */
  compute?: LlmCompute;
  compute_detail?: string;
}

export type LlmCompute = "gpu" | "cpu" | "partial" | "unknown";

// ---------------------------------------------------------------------------
// Assistant model provider (bring your own model)
// ---------------------------------------------------------------------------

export type LlmProvider = "local" | "openai" | "openai_compatible" | "azure_openai" | "anthropic";
export type EmbeddingSource = "local" | "provider" | "none";

export interface LlmProviderConfig {
  provider: LlmProvider;
  model: string;
  base_url: string;
  /** "********" when a key is stored; send it back unchanged to keep it. */
  api_key: string;
  api_key_set?: boolean;
  api_version: string;
  embedding_source: EmbeddingSource;
  embedding_model: string;
  request_timeout_s: number;
  max_tokens: number;
}

export interface LlmProviderInfo {
  value: LlmProvider;
  label: string;
  needs_api_key: boolean;
  needs_base_url: boolean;
  has_embeddings: boolean;
}

export interface LlmProviderSettings {
  config: LlmProviderConfig;
  /** "ui" when an admin saved a choice; "env" for the LLM_* environment defaults. */
  source: "ui" | "env";
  providers?: LlmProviderInfo[];
  embedding_sources?: EmbeddingSource[];
}

export interface LlmProviderTestResult {
  ok: boolean;
  detail: string;
  chat_ms?: number;
  embedding_dims?: number | null;
}

export interface AuthSession {
  token: string;
  /** epoch seconds; idle timeout, extended on every request */
  expires_at: number;
}

export interface AuthSessionInfo {
  authenticated: boolean;
  method: "open" | "token" | "session";
  login_enabled: boolean;
  password_set_at: string | null;
}
