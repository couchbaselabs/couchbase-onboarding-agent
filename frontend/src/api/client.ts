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
  RollbackOptions,
  RollbackPlan,
  SourceConnectionConfig,
  SourceTypeInfo,
  SourceTopologySnapshot,
  ValidationReport,
} from "./types";

/** Same-origin by default: the bundled nginx (and `npm run dev`'s proxy) forward
 * /api and /ws to the backend. Set VITE_API_BASE_URL only when the UI is served
 * from somewhere else. */
const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";

const TOKEN_KEY = "cb-onboarding-api-token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}

/** Fired when the backend rejects the current token; AuthGate listens for it. */
export const AUTH_REQUIRED_EVENT = "cb-auth-required";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options?.headers || {}),
    },
  });
  if (resp.status === 401) {
    window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // ignore
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/** WebSocket URL for a path, carrying the API token as a query parameter
 * (browsers can't set headers on a WebSocket). */
export function wsUrl(path: string): string {
  const explicit = import.meta.env.VITE_WS_BASE_URL as string | undefined;
  let base: string;
  if (explicit) base = explicit;
  else if (BASE) base = BASE.replace(/^http/, "ws");
  else base = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
  return `${base}${path}`;
}

/** The API token travels as a websocket subprotocol ("coa-auth", <token>) --
 * a header, not a query string -- so it never lands in a URL, a browser
 * history entry or a proxy access log. */
export function wsProtocols(): string[] | undefined {
  const token = getToken();
  return token ? ["coa-auth", token] : undefined;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  publicConfig: () => request<{ auth_required: boolean; version: string }>("/api/config"),

  sourceTypes: () => request<SourceTypeInfo[]>("/api/source-types"),

  testSourceConnection: (config: SourceConnectionConfig) =>
    post<SourceTopologySnapshot>("/api/sources/test-connection", config),

  testDestinationConnection: (config: CouchbaseConnectionConfig) =>
    post<CouchbaseTopologySnapshot>("/api/sources/test-destination", config),

  recommendMode: (cutoverPlan: "cutover" | "phased", sourceTopology: SourceTopologySnapshot, concurrency: number) =>
    post<ReplicationModeRecommendationResponse>("/api/agent/recommend-replication-mode", {
      cutover_plan: cutoverPlan, source_topology: sourceTopology, concurrency,
    }),

  createMigration: (plan: MigrationPlanCreate) => post<MigrationRecord>("/api/migrations", plan),

  listMigrations: () => request<MigrationRecord[]>("/api/migrations"),

  getMigration: (id: string) => request<MigrationRecord>(`/api/migrations/${id}`),

  validateMigration: (id: string) => post<ValidationReport>(`/api/migrations/${id}/validate`),

  approveMigration: (id: string, approvedBy: string) =>
    post<MigrationRecord>(`/api/migrations/${id}/approve`, { migration_id: id, approved_by: approvedBy }),

  startMigration: (id: string) => post<MigrationRecord>(`/api/migrations/${id}/start`),

  stopReplication: (id: string, performCutover: boolean) =>
    post<MigrationRecord>(`/api/migrations/${id}/replication/stop`, {
      migration_id: id, perform_cutover: performCutover,
    }),

  resumeMigration: (id: string) => post<MigrationRecord>(`/api/migrations/${id}/resume`),

  reconcile: (id: string, force = false) =>
    post<MigrationRecord>(`/api/migrations/${id}/reconcile`, { migration_id: id, force }),

  rollbackPlan: (id: string, options: RollbackOptions) =>
    post<RollbackPlan>(`/api/migrations/${id}/rollback/plan`, { migration_id: id, ...options }),

  rollbackMigration: (id: string, options: RollbackOptions) =>
    post<MigrationRecord>(`/api/migrations/${id}/rollback`, { migration_id: id, ...options }),

  startFallback: (id: string) => post<MigrationRecord>(`/api/migrations/${id}/fallback/start`),

  stopFallback: (id: string, fallBackToSource: boolean) =>
    post<MigrationRecord>(`/api/migrations/${id}/fallback/stop`, {
      migration_id: id, fall_back_to_source: fallBackToSource,
    }),

  failback: (id: string) => post<MigrationRecord>(`/api/migrations/${id}/failback`),

  deleteMigration: (id: string) => request<{ deleted: boolean }>(`/api/migrations/${id}`, { method: "DELETE" }),

  getLogs: (id: string) => request<{ log_tail: string[] }>(`/api/stats/${id}/logs`),

  chat: (message: string, migrationId?: string) =>
    post<AgentChatResponse>("/api/agent/chat", { message, migration_id: migrationId, use_memory: true }),

  agentStatus: () => request<AgentStatusResponse>("/api/agent/status"),
};

export type { MigrationStrategy, ContainerMigrationSpec };
export { ApiError };
