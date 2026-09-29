import type { CouchbaseConnectionConfig } from "@/api/types";
import TlsFields from "./TlsFields";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  // Wrapping label: the control inside is labelled without needing ids.
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

export default function CouchbaseConfigForm({
  value,
  onChange,
}: {
  value: CouchbaseConnectionConfig;
  onChange: (patch: Partial<CouchbaseConnectionConfig>) => void;
}) {
  return (
    <div className="form-grid">
      <Field label="Friendly name">
        <input value={value.label} onChange={(e) => onChange({ label: e.target.value })} />
      </Field>
      <Field label="Connection string">
        <input
          value={value.connection_string}
          onChange={(e) => onChange({ connection_string: e.target.value })}
          placeholder="couchbases://cb.xxxxx.cloud.couchbase.com"
        />
      </Field>
      <div className="two-col">
        <Field label="Username">
          <input value={value.username} onChange={(e) => onChange({ username: e.target.value })} />
        </Field>
        <Field label="Password">
          <input type="password" value={value.password} onChange={(e) => onChange({ password: e.target.value })} />
        </Field>
      </div>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={!!value.is_capella}
          onChange={(e) =>
            onChange({ is_capella: e.target.checked, use_tls: e.target.checked ? true : value.use_tls })
          }
        />
        This endpoint is a Couchbase Capella cluster
      </label>
      {value.is_capella && (
        <div className="two-col">
          <Field label="Capella project ID (optional)">
            <input
              value={value.capella_project_id || ""}
              onChange={(e) => onChange({ capella_project_id: e.target.value })}
            />
          </Field>
          <Field label="Capella cluster ID (optional)">
            <input
              value={value.capella_cluster_id || ""}
              onChange={(e) => onChange({ capella_cluster_id: e.target.value })}
            />
          </Field>
        </div>
      )}
      {!value.is_capella && (
        <label className="checkbox-row">
          <input type="checkbox" checked={!!value.use_tls} onChange={(e) => onChange({ use_tls: e.target.checked })} />
          Use TLS
        </label>
      )}
      {(value.is_capella || value.use_tls) && (
        <TlsFields caPem={value.ca_cert_pem} skipVerify={value.tls_skip_verify} onChange={onChange} />
      )}
      <div className="field-hint">
        Allow-list this agent's IP address on the destination cluster before testing the connection.
        Set <code>CAPELLA_API_TOKEN</code>/<code>CAPELLA_ORG_ID</code> on the backend plus the project/cluster
        ID above if you want the destination bucket auto-provisioned.
      </div>
    </div>
  );
}
