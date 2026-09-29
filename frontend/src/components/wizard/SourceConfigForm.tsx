import { Fragment } from "react";
import type { SourceConnectionConfig, SourceType } from "@/api/types";
import { SOURCE_TYPE_LABELS, SOURCE_TYPE_SUPPORT } from "@/theme/tokens";
import TlsFields from "./TlsFields";

const SOURCE_TYPES: SourceType[] = [
  "mongodb", "dynamodb", "redis", "cassandra", "cosmosdb",
  "couchbase", "couchbase_enterprise", "couchbase_capella",
];

const PLACEHOLDER: Partial<Record<SourceType, string>> = {
  mongodb: "mongodb://host1,host2/?replicaSet=rs0",
  redis: "redis://host:6379",
  cassandra: "host1,host2,host3",
  couchbase: "couchbase://host1,host2",
  couchbase_enterprise: "couchbase://host1,host2",
  couchbase_capella: "couchbases://cb.xxxxxxxxxxxxxxxx.cloud.couchbase.com",
};

const CB_FAMILY: SourceType[] = ["couchbase", "couchbase_enterprise", "couchbase_capella"];
const CB_SELF_MANAGED: SourceType[] = ["couchbase", "couchbase_enterprise"];
const HAS_CONNECTION_STRING: SourceType[] = ["mongodb", "redis", "cassandra", ...CB_FAMILY];
const HAS_DATABASE_FIELD: SourceType[] = ["mongodb", "cassandra", "cosmosdb", ...CB_FAMILY];
const HAS_USERNAME_PASSWORD: SourceType[] = ["mongodb", "redis", "cassandra", ...CB_FAMILY];

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  // Wrapping label: the control inside is labelled without needing ids.
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

function databaseFieldLabel(t: SourceType): string {
  if (t === "cassandra") return "Keyspace";
  if (CB_FAMILY.includes(t)) return "Bucket";
  return "Database";
}

export default function SourceConfigForm({
  value,
  onChange,
}: {
  value: SourceConnectionConfig;
  onChange: (patch: Partial<SourceConnectionConfig>) => void;
}) {
  const t = value.source_type;
  const support = SOURCE_TYPE_SUPPORT[t];
  const isCapella = t === "couchbase_capella";

  return (
    <div className="form-grid">
      <Field label="Source type">
        <select
          value={t}
          onChange={(e) => {
            const next = e.target.value as SourceType;
            onChange({
              source_type: next,
              // Capella mandates TLS -- don't leave a user on an
              // unencrypted connection just because a prior source type
              // left the toggle unchecked.
              use_tls: next === "couchbase_capella" ? true : value.use_tls,
            });
          }}
        >
          {SOURCE_TYPES.map((v) => (
            <Fragment key={v}>
              {v === "couchbase" && (
                <option disabled style={{ borderTop: "1px solid var(--text)", color: "transparent" }}>
                  {"─".repeat(24)}
                </option>
              )}
              <option value={v}>{SOURCE_TYPE_LABELS[v]}</option>
            </Fragment>
          ))}
        </select>
      </Field>

      {support && (
        <div className="field-hint" style={{ marginTop: -6 }}>
          {support.platform} &middot; {support.versions}
        </div>
      )}

      <Field label="Friendly name">
        <input value={value.label} onChange={(e) => onChange({ label: e.target.value })} placeholder="Production MongoDB" />
      </Field>

      {HAS_CONNECTION_STRING.includes(t) && (
        <Field label={t === "cassandra" ? "Contact points (comma-separated)" : "Connection string"}>
          <input
            value={value.connection_string || ""}
            onChange={(e) => onChange({ connection_string: e.target.value })}
            placeholder={PLACEHOLDER[t]}
          />
        </Field>
      )}

      {HAS_DATABASE_FIELD.includes(t) && (
        <Field label={databaseFieldLabel(t)}>
          <input value={value.database || ""} onChange={(e) => onChange({ database: e.target.value })} />
        </Field>
      )}

      {t === "cassandra" && (
        <div className="two-col">
          <Field label="Port">
            <input
              type="number"
              value={value.cassandra_port ?? 9042}
              onChange={(e) => onChange({ cassandra_port: Number(e.target.value) })}
            />
          </Field>
          <Field label="Local datacenter">
            <input
              value={value.cassandra_datacenter || ""}
              onChange={(e) => onChange({ cassandra_datacenter: e.target.value })}
              placeholder="datacenter1"
            />
          </Field>
        </div>
      )}

      {t === "redis" && (
        <Field label="Database index">
          <input
            type="number"
            value={value.redis_db_index ?? 0}
            onChange={(e) => onChange({ redis_db_index: Number(e.target.value) })}
          />
        </Field>
      )}

      {HAS_USERNAME_PASSWORD.includes(t) && (
        <div className="two-col">
          <Field label="Username">
            <input value={value.username || ""} onChange={(e) => onChange({ username: e.target.value })} />
          </Field>
          <Field label="Password">
            <input
              type="password"
              value={value.password || ""}
              onChange={(e) => onChange({ password: e.target.value })}
            />
          </Field>
        </div>
      )}

      {(t === "mongodb" || t === "redis" || t === "cassandra" || CB_SELF_MANAGED.includes(t)) && (
        <label className="checkbox-row">
          <input type="checkbox" checked={!!value.use_tls} onChange={(e) => onChange({ use_tls: e.target.checked })} />
          Use TLS
        </label>
      )}

      {isCapella && (
        <div className="field-hint">TLS is required and always enabled for Capella.</div>
      )}

      {(isCapella || (!!value.use_tls && (t === "mongodb" || t === "redis" || t === "cassandra" || CB_SELF_MANAGED.includes(t)))) && (
        <TlsFields caPem={value.ca_cert_pem} skipVerify={value.tls_skip_verify} onChange={onChange} />
      )}

      {CB_SELF_MANAGED.includes(t) && (
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={!!value.couchbase_external_network}
            onChange={(e) => onChange({ couchbase_external_network: e.target.checked })}
          />
          <span>
            Cluster is on a cloud VM or Kubernetes (EC2, GKE, etc.)
            <div className="field-hint" style={{ marginTop: 3 }}>
              Enable if the connection succeeds at first but then fails with "connection
              refused" once the SDK has the full cluster map. Also requires External Address /
              alternate addressing to already be configured on the source cluster itself
              (Couchbase Web Console &rarr; Server Nodes).
            </div>
          </span>
        </label>
      )}

      {t === "dynamodb" && (
        <>
          <Field label="AWS region">
            <input value={value.aws_region || ""} onChange={(e) => onChange({ aws_region: e.target.value })} placeholder="us-east-1" />
          </Field>
          <div className="two-col">
            <Field label="Access key ID">
              <input
                value={value.aws_access_key_id || ""}
                onChange={(e) => onChange({ aws_access_key_id: e.target.value })}
              />
            </Field>
            <Field label="Secret access key">
              <input
                type="password"
                value={value.aws_secret_access_key || ""}
                onChange={(e) => onChange({ aws_secret_access_key: e.target.value })}
              />
            </Field>
          </div>
          <Field label="Session token (optional)">
            <input
              type="password"
              value={value.aws_session_token || ""}
              onChange={(e) => onChange({ aws_session_token: e.target.value })}
            />
          </Field>
          <Field label="Endpoint override (optional -- DynamoDB Local / VPC endpoint)">
            <input
              value={value.dynamodb_endpoint_url || ""}
              onChange={(e) => onChange({ dynamodb_endpoint_url: e.target.value })}
              placeholder="Leave blank for real AWS"
            />
          </Field>
        </>
      )}

      {t === "cosmosdb" && (
        <>
          <Field label="Account endpoint">
            <input
              value={value.cosmos_endpoint || ""}
              onChange={(e) => onChange({ cosmos_endpoint: e.target.value })}
              placeholder="https://my-account.documents.azure.com:443/"
            />
          </Field>
          <Field label="Primary/secondary key">
            <input type="password" value={value.cosmos_key || ""} onChange={(e) => onChange({ cosmos_key: e.target.value })} />
          </Field>
        </>
      )}

      {CB_FAMILY.includes(t) && (
        <div className="field-hint">
          Couchbase-to-Couchbase bulk copy uses cbbackupmgr. Continuous sync uses XDCR from a
          self-managed Enterprise Edition source; from Community Edition or Capella it polls the
          Query service for changed documents (create a <code>META().cas</code> index on large
          collections first). Keys, scopes and collections are kept as-is.
        </div>
      )}

      <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 4 }}>
        Allow-list this agent's IP address on the source database before testing the connection --
        see the README for the exact reachability requirements per source type.
      </div>
    </div>
  );
}
