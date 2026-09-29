/** Color references for places CSS classes can't reach (inline SVG, Recharts,
 * lucide icon props). Every value is a CSS custom property from theme.css, so
 * the same code renders correctly in light and dark mode. */
export const colors = {
  cbRed: "var(--red)",
  cbRedDim: "var(--red-bg)",
  cbRedBright: "var(--red-dim)",
  cbTeal: "var(--teal)",
  cbAmber: "var(--amber)",
  cbGreen: "var(--green)",
  cbBlue: "var(--blue)",
  bg0: "var(--bg)",
  bg1: "var(--card-bg)",
  bg2: "var(--card-bg-alt)",
  bg3: "var(--bg-hover)",
  borderSubtle: "var(--border)",
  borderStrong: "var(--border-strong)",
  textPrimary: "var(--text)",
  textSecondary: "var(--text-muted)",
  textMuted: "var(--text-faint)",
} as const;

/** A translucent wash of `color` (any CSS color, including var()) for badge
 * and banner backgrounds. */
export const tint = (color: string, percent = 14): string =>
  `color-mix(in srgb, ${color} ${percent}%, transparent)`;

export const statusColor = (status: string): string => {
  switch (status) {
    case "complete":
    case "validated":
    case "healthy":
    case "rolled_back":
      return colors.cbGreen;
    case "awaiting_approval":
    case "validation_failed":
    case "rolling_back":
    case "fell_back":
    case "stopped":
      return colors.cbAmber;
    case "failed":
      return colors.cbRedBright;
    case "migrating":
    case "replicating":
    case "validating":
    case "verifying":
    case "fallback_syncing":
      return colors.cbTeal;
    default:
      return colors.cbBlue;
  }
};

export type Tone = "success" | "danger" | "warning" | "info" | "neutral";

/** Badge tone for a migration phase (AOM badge classes: badge-<tone>). */
export const phaseTone = (phase: string): Tone => {
  switch (phase) {
    case "complete":
    case "validated":
    case "rolled_back":
      return "success";
    case "awaiting_approval":
    case "validation_failed":
    case "rolling_back":
    case "fell_back":
    case "stopped":
      return "warning";
    case "failed":
      return "danger";
    case "migrating":
    case "replicating":
    case "validating":
    case "verifying":
    case "fallback_syncing":
      return "info";
    default:
      return "neutral";
  }
};

export const PHASE_LABELS: Record<string, string> = {
  fallback_syncing: "fallback syncing",
  fell_back: "fell back to source",
};

export const phaseLabel = (phase: string): string => PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");

export const SOURCE_TYPE_LABELS: Record<string, string> = {
  mongodb: "MongoDB",
  dynamodb: "Amazon DynamoDB",
  redis: "Redis",
  cassandra: "Apache Cassandra",
  cosmosdb: "Microsoft Azure Cosmos DB",
  couchbase: "Couchbase (Community Edition)",
  couchbase_enterprise: "Couchbase (Enterprise Edition)",
  couchbase_capella: "Couchbase Capella",
};

export const CDC_METHOD_LABELS: Record<string, string> = {
  change_stream: "MongoDB change streams",
  stream_shards: "DynamoDB Streams",
  change_feed: "Cosmos DB change feed",
  writetime_poll: "WRITETIME() polling",
  keyspace_events: "Keyspace notifications + re-read",
  xdcr: "XDCR",
  cas_poll: "CAS polling (Query service)",
};

/** Platform(s) each source connector talks to, the version range this app has
 * been built/tested against, and a compact form of that range for tight
 * spaces (sidebar). Single source for the wizard's caption and the sidebar. */
export interface SourceTypeSupport {
  platform: string;
  versions: string;
  versionShort: string;
}

export const SOURCE_TYPE_SUPPORT: Record<string, SourceTypeSupport> = {
  mongodb: {
    platform: "MongoDB Atlas or self-managed (Community & Enterprise)",
    versions: "MongoDB Server 3.6 - 8.0",
    versionShort: "3.6 - 8.0",
  },
  dynamodb: {
    platform: "Amazon DynamoDB (AWS-managed) or DynamoDB Local",
    versions: "Current DynamoDB API -- SaaS, no server version to track",
    versionShort: "SaaS",
  },
  redis: {
    platform: "Redis OSS, Redis Cloud, or Redis Enterprise",
    versions: "Redis 5.0+",
    versionShort: "5.0+",
  },
  cassandra: {
    platform: "Apache Cassandra or DataStax Enterprise",
    versions: "Cassandra 2.1 - 4.x",
    versionShort: "2.1 - 4.x",
  },
  cosmosdb: {
    platform: "Azure Cosmos DB, SQL (Core) API -- SaaS",
    versions: "Current Cosmos DB API -- SaaS, no server version to track",
    versionShort: "SaaS",
  },
  couchbase: {
    platform: "Couchbase Server, self-managed (Community Edition)",
    versions: "Couchbase Server 7.2 - 8.0.2",
    versionShort: "7.2 - 8.0.2",
  },
  couchbase_enterprise: {
    platform: "Couchbase Server, self-managed (Enterprise Edition)",
    versions: "Couchbase Server 7.2 - 8.0.2",
    versionShort: "7.2 - 8.0.2",
  },
  couchbase_capella: {
    platform: "Couchbase Capella -- SaaS",
    versions: "Current Couchbase Capella service -- SaaS, no self-hosted version to track",
    versionShort: "SaaS",
  },
};
