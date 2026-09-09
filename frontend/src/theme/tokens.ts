/** JS-side mirror of theme.css custom properties, for use where CSS variables
 * aren't convenient (e.g. computed stroke colors inside an inline SVG diagram). */
export const colors = {
  cbRed: "#EA2328",
  cbRedDim: "#7A1215",
  cbRedBright: "#FF4B4F",
  cbTeal: "#00A7B5",
  cbAmber: "#F2A900",
  cbGreen: "#2ECC71",
  cbBlue: "#4C9AFF",
  bg0: "#0B0E14",
  bg1: "#12161F",
  bg2: "#191E2A",
  bg3: "#232936",
  borderSubtle: "#2A3140",
  borderStrong: "#3A4256",
  textPrimary: "#E8EAED",
  textSecondary: "#9AA4B2",
  textMuted: "#6B7484",
} as const;

export const statusColor = (status: string): string => {
  switch (status) {
    case "complete":
    case "validated":
    case "healthy":
      return colors.cbGreen;
    case "awaiting_approval":
    case "validation_failed":
      return colors.cbAmber;
    case "failed":
    case "rolled_back":
      return colors.cbRedBright;
    case "migrating":
    case "replicating":
    case "validating":
    case "verifying":
      return colors.cbTeal;
    default:
      return colors.cbBlue;
  }
};

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

/** Platform(s) each source connector talks to, the version range this app has
 * been built/tested against, and a compact form of that range for tight
 * spaces (sidebar). Single canonical source for both the wizard's source-type
 * caption and the sidebar's "Migrations Supported" list, so the two can't
 * drift out of sync. For the fully managed cloud services (DynamoDB, Cosmos
 * DB, Capella) there's no self-hosted version to track; for self-managed
 * Couchbase, the range matches this app's own destination-side supported
 * range (see backend Settings.min/max_supported_cb_version), since source and
 * destination are the same product family there. */
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
