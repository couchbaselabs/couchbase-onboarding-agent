import type { CouchbaseConnectionConfig } from "@/api/types";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <label style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>{label}</label>
      {children}
    </div>
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
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
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
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="Username">
          <input value={value.username} onChange={(e) => onChange({ username: e.target.value })} />
        </Field>
        <Field label="Password">
          <input type="password" value={value.password} onChange={(e) => onChange({ password: e.target.value })} />
        </Field>
      </div>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
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
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
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
        <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
          <input type="checkbox" checked={!!value.use_tls} onChange={(e) => onChange({ use_tls: e.target.checked })} />
          Use TLS
        </label>
      )}
      <div style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
        Allow-list this agent's IP address on the destination cluster before testing the connection.
        Set <code>CAPELLA_API_TOKEN</code>/<code>CAPELLA_ORG_ID</code> in <code>.env</code> plus the project/cluster
        ID above if you want the destination bucket auto-provisioned.
      </div>
    </div>
  );
}
