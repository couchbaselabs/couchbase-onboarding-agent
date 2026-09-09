import type {
  AgentChatResponse,
  AgentStatusResponse,
  ContainerMigrationSpec,
  CouchbaseConnectionConfig,
  CouchbaseTopologySnapshot,
  MigrationPlanCreate,
  MigrationRecord,
  MigrationStrategy,
  ReplicationModeRecommendationResponse,
  SourceConnectionConfig,
  SourceTopologySnapshot,
  ValidationReport,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers || {}) },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      // ignore
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  sourceTypes: () => request<{ value: string; label: string; fields: string[] }[]>("/api/source-types"),

  testSourceConnection: (config: SourceConnectionConfig) =>
    request<SourceTopologySnapshot>("/api/sources/test-connection", {
      method: "POST",
      body: JSON.stringify(config),
    }),

  testDestinationConnection: (config: CouchbaseConnectionConfig) =>
    request<CouchbaseTopologySnapshot>("/api/sources/test-destination", {
      method: "POST",
      body: JSON.stringify(config),
    }),

  recommendMode: (
    cutoverPlan: "cutover" | "phased",
    sourceTopology: SourceTopologySnapshot,
    concurrency: number,
  ) =>
    request<ReplicationModeRecommendationResponse>("/api/agent/recommend-replication-mode", {
      method: "POST",
      body: JSON.stringify({ cutover_plan: cutoverPlan, source_topology: sourceTopology, concurrency }),
    }),

  createMigration: (plan: MigrationPlanCreate) =>
    request<MigrationRecord>("/api/migrations", { method: "POST", body: JSON.stringify(plan) }),

  listMigrations: () => request<MigrationRecord[]>("/api/migrations"),

  getMigration: (id: string) => request<MigrationRecord>(`/api/migrations/${id}`),

  validateMigration: (id: string) =>
    request<ValidationReport>(`/api/migrations/${id}/validate`, { method: "POST" }),

  approveMigration: (id: string, approvedBy: string) =>
    request<MigrationRecord>(`/api/migrations/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ migration_id: id, approved_by: approvedBy }),
    }),

  startMigration: (id: string) =>
    request<MigrationRecord>(`/api/migrations/${id}/start`, { method: "POST" }),

  stopReplication: (id: string, performCutover: boolean) =>
    request<MigrationRecord>(`/api/migrations/${id}/replication/stop`, {
      method: "POST",
      body: JSON.stringify({ migration_id: id, perform_cutover: performCutover }),
    }),

  rollbackMigration: (id: string, reason: string, purgeDestinationData = true) =>
    request<MigrationRecord>(`/api/migrations/${id}/rollback`, {
      method: "POST",
      body: JSON.stringify({ migration_id: id, reason, purge_destination_data: purgeDestinationData }),
    }),

  deleteMigration: (id: string) => request<{ deleted: boolean }>(`/api/migrations/${id}`, { method: "DELETE" }),

  getLogs: (id: string) => request<{ log_tail: string[] }>(`/api/stats/${id}/logs`),

  chat: (message: string, migrationId?: string) =>
    request<AgentChatResponse>("/api/agent/chat", {
      method: "POST",
      body: JSON.stringify({ message, migration_id: migrationId, use_memory: true }),
    }),

  agentStatus: () => request<AgentStatusResponse>("/api/agent/status"),
};

export type { MigrationStrategy, ContainerMigrationSpec };
export { ApiError };
