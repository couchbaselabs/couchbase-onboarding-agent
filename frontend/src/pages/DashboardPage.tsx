import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { MigrationRecord } from "@/api/types";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import { statusColor, SOURCE_TYPE_LABELS } from "@/theme/tokens";

export default function DashboardPage() {
  const [migrations, setMigrations] = useState<MigrationRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const { records: liveRecords } = useMigrationSocket("*");

  useEffect(() => {
    api
      .listMigrations()
      .then(setMigrations)
      .finally(() => setLoading(false));
  }, []);

  const merged = migrations.map((m) => liveRecords[m.migration_id] ?? m);

  return (
    <div style={{ padding: 32, maxWidth: 1100 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Migrations</h1>
          <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: 4 }}>
            MongoDB, DynamoDB, Redis, Cassandra, and Cosmos DB onboarding into Couchbase.
          </p>
        </div>
        <Link to="/new" className="cb-btn cb-btn-primary">
          New migration
        </Link>
      </div>

      {loading && <div style={{ color: "var(--text-muted)" }}>Loading...</div>}

      {!loading && merged.length === 0 && (
        <div className="cb-card" style={{ textAlign: "center", padding: 48 }}>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 6 }}>No migrations yet</div>
          <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginBottom: 16 }}>
            Start one from a source database to Couchbase.
          </div>
          <Link to="/new" className="cb-btn cb-btn-primary">
            New migration
          </Link>
        </div>
      )}

      {merged.length > 0 && (
        <div className="cb-card" style={{ padding: 0, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", borderBottom: "1px solid var(--border-subtle)" }}>
                {["Name", "Source", "Destination bucket", "Phase", "Docs migrated", "Updated"].map((h) => (
                  <th key={h} style={{ padding: "10px 16px", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {merged
                .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
                .map((m) => (
                  <tr key={m.migration_id} style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                    <td style={{ padding: "12px 16px" }}>
                      <Link to={`/migrations/${m.migration_id}`} style={{ fontWeight: 600 }}>
                        {m.plan.name}
                      </Link>
                    </td>
                    <td style={{ padding: "12px 16px", color: "var(--text-secondary)" }}>
                      {SOURCE_TYPE_LABELS[m.plan.source.source_type]}
                    </td>
                    <td style={{ padding: "12px 16px", color: "var(--text-secondary)" }}>{m.plan.destination_bucket}</td>
                    <td style={{ padding: "12px 16px" }}>
                      <span className="cb-badge" style={{ background: `${statusColor(m.phase)}22`, color: statusColor(m.phase) }}>
                        {m.phase.replace(/_/g, " ")}
                      </span>
                    </td>
                    <td style={{ padding: "12px 16px" }}>{m.stats.docs_migrated.toLocaleString()}</td>
                    <td style={{ padding: "12px 16px", color: "var(--text-muted)" }}>
                      {new Date(m.updated_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
