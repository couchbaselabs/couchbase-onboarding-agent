import type {
  AgentChatResponse,
  AgentStatusResponse,
  AuthSession,
  AuthSessionInfo,
  PermissionDef,
  RbacRole,
  RbacUser,
  LdapConfig,
  LdapSettingsResponse,
  LdapTestResult,
  LogForwardConfig,
  LogForwardSettingsResponse,
  LogForwardStatus,
  ServingCertStatus,
  TrustedCerts,
  LlmProviderConfig,
  LlmProviderSettings,
  LlmProviderTestResult,
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

export interface PublicConfig {
  /** A bearer (session or API token) is needed for /api. */
  auth_required: boolean;
  /** The admin password login is on (ADMIN_LOGIN_ENABLED). */
  login_enabled?: boolean;
  /** No administrator password yet: show the first-run setup. */
  setup_required?: boolean;
  /** Directory (LDAP) sign-in is configured and on. */
  ldap_enabled?: boolean;
  /** Setup must also present API_AUTH_TOKEN. */
  bootstrap_token_required?: boolean;
  version: string;
  /** false when the deployment runs without the assistant (LLM_ENABLED=false). */
  llm_enabled?: boolean;
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  publicConfig: () => request<PublicConfig>("/api/config"),

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

  // --- admin login ---
  authSetup: (password: string, bootstrapToken?: string) =>
    post<AuthSession>("/api/auth/setup", { password, bootstrap_token: bootstrapToken ?? "" }),
  authLogin: (username: string, password: string) =>
    post<AuthSession>("/api/auth/login", { username, password }),
  authLogout: () => post<void>("/api/auth/logout"),
  authSession: () => request<AuthSessionInfo>("/api/auth/session"),
  // RBAC (users:manage)
  listPermissions: () => request<PermissionDef[]>("/api/auth/permissions"),
  listRoles: () => request<RbacRole[]>("/api/auth/roles"),
  createRole: (body: { name: string; description: string; permissions: string[] }) =>
    post<RbacRole>("/api/auth/roles", body),
  updateRole: (name: string, body: { description?: string; permissions?: string[] }) =>
    request<RbacRole>(`/api/auth/roles/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteRole: (name: string) => request<void>(`/api/auth/roles/${encodeURIComponent(name)}`, { method: "DELETE" }),
  listUsers: () => request<RbacUser[]>("/api/auth/users"),
  createUser: (body: { username: string; password: string; role: string }) => post<RbacUser>("/api/auth/users", body),
  updateUser: (username: string, body: { role?: string; disabled?: boolean; password?: string }) =>
    request<RbacUser>(`/api/auth/users/${encodeURIComponent(username)}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteUser: (username: string) =>
    request<void>(`/api/auth/users/${encodeURIComponent(username)}`, { method: "DELETE" }),
  // Certificates (settings:write)
  trustedCerts: () => request<TrustedCerts>("/api/settings/certificates/trusted"),
  addTrustedCert: (pem: string) => post<TrustedCerts & { added: unknown[] }>("/api/settings/certificates/trusted", { pem }),
  removeTrustedCert: (fingerprint: string) =>
    request<void>(`/api/settings/certificates/trusted/${fingerprint}`, { method: "DELETE" }),
  servingCert: () => request<ServingCertStatus>("/api/settings/certificates/server"),
  installServingCert: (certificate_pem: string, private_key_pem: string) =>
    request<ServingCertStatus>("/api/settings/certificates/server", {
      method: "PUT",
      body: JSON.stringify({ certificate_pem, private_key_pem }),
    }),
  removeServingCert: () => request<ServingCertStatus>("/api/settings/certificates/server", { method: "DELETE" }),
  // LDAP (settings:write)
  ldapSettings: () => request<LdapSettingsResponse>("/api/settings/ldap"),
  saveLdap: (cfg: LdapConfig) =>
    request<{ config: LdapConfig }>("/api/settings/ldap", { method: "PUT", body: JSON.stringify(cfg) }),
  testLdap: (cfg: LdapConfig, test_username?: string, test_password?: string) =>
    post<LdapTestResult>("/api/settings/ldap/test", { ...cfg, test_username, test_password }),
  // Log forwarding (settings:write)
  logForwarding: () => request<LogForwardSettingsResponse>("/api/settings/log-forwarding"),
  saveLogForwarding: (cfg: LogForwardConfig) =>
    request<{ config: LogForwardConfig; status: LogForwardStatus }>("/api/settings/log-forwarding", {
      method: "PUT",
      body: JSON.stringify(cfg),
    }),
  testLogForwarding: (cfg: LogForwardConfig) =>
    post<{ ok: boolean; destination: string; ms: number }>("/api/settings/log-forwarding/test", cfg),
  changePassword: (currentPassword: string, newPassword: string) =>
    post<void>("/api/auth/password", { current_password: currentPassword, new_password: newPassword }),

  // --- assistant model provider ---
  getProvider: () => request<LlmProviderSettings>("/api/agent/provider"),
  testProvider: (config: LlmProviderConfig) => post<LlmProviderTestResult>("/api/agent/provider/test", config),
  saveProvider: (config: LlmProviderConfig) =>
    request<LlmProviderSettings>("/api/agent/provider", { method: "PUT", body: JSON.stringify(config) }),
  resetProvider: () => request<LlmProviderSettings>("/api/agent/provider", { method: "DELETE" }),
};

export type { MigrationStrategy, ContainerMigrationSpec };
export { ApiError };
