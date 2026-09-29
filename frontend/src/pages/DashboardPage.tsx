import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { MigrationRecord } from "@/api/types";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import StatCard from "@/components/dashboard/StatCard";
import { formatUtc } from "@/utils/time";
import { phaseLabel, phaseTone, SOURCE_TYPE_LABELS } from "@/theme/tokens";

const ACTIVE = new Set(["validating", "migrating", "replicating", "verifying", "fallback_syncing", "rolling_back"]);
const ATTENTION = new Set(["failed", "validation_failed", "stopped"]);

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

  const merged = migrations
    .map((m) => liveRecords[m.migration_id] ?? m)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

  const active = merged.filter((m) => ACTIVE.has(m.phase));
  const syncing = merged.filter((m) => m.phase === "replicating" || m.phase === "fallback_syncing");
  const complete = merged.filter((m) => m.phase === "complete");
  const attention = merged.filter((m) => ATTENTION.has(m.phase));
  const moved = merged.reduce((n, m) => n + m.stats.docs_migrated + m.stats.mutations_replicated, 0);

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">Migrations</h1>
          <p className="page-subtitle">
            MongoDB, DynamoDB, Redis, Cassandra, Cosmos DB, and Couchbase onboarding into Couchbase Server or Capella.
          </p>
        </div>
        <Link to="/new" className="btn btn-primary">
          New migration
        </Link>
      </div>

      {loading && <div className="loading-note">Loading...</div>}

      {!loading && (
        <div className="stat-grid">
          <StatCard
            label="Migrations"
            value={merged.length.toLocaleString()}
            sub={`${active.length} running, ${complete.length} complete`}
          />
          <StatCard
            label="Continuous sync"
            value={syncing.length.toLocaleString()}
            sub={syncing.length ? "streaming changes now" : "no migration streaming"}
          />
          <StatCard label="Documents + changes moved" value={compact(moved)} sub="across all migrations" />
          <StatCard
            label="Needs attention"
            value={attention.length.toLocaleString()}
            sub={attention.length ? "failed, stopped, or failed validation" : "nothing needs attention"}
            valueColor={attention.length ? "var(--red-dim)" : undefined}
          />
        </div>
      )}

      {!loading && merged.length === 0 && (
        <div className="card empty-state">
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text)", marginBottom: 6 }}>No migrations yet</div>
          <div style={{ marginBottom: 16 }}>Start one from a source database to Couchbase.</div>
          <Link to="/new" className="btn btn-primary">
            New migration
          </Link>
        </div>
      )}

      {merged.length > 0 && (
        <>
          <div className="section-row">
            <h2 className="section-heading">All migrations</h2>
            <span className="cell-muted" style={{ fontSize: 12 }}>
              Updates live
            </span>
          </div>
          <div className="card">
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Source</th>
                    <th>Destination bucket</th>
                    <th>Phase</th>
                    <th className="num">Docs + changes</th>
                    <th>Updated (UTC)</th>
                  </tr>
                </thead>
                <tbody>
                  {merged.map((m) => (
                    <tr key={m.migration_id}>
                      <td>
                        <Link to={`/migrations/${m.migration_id}`} className="row-link">
                          {m.plan.name}
                        </Link>
                      </td>
                      <td className="cell-muted">{SOURCE_TYPE_LABELS[m.plan.source.source_type]}</td>
                      <td className="cell-mono cell-muted">{m.plan.destination_bucket}</td>
                      <td>
                        <span className={`badge badge-${phaseTone(m.phase)}`}>{phaseLabel(m.phase)}</span>
                      </td>
                      <td className="num">{(m.stats.docs_migrated + m.stats.mutations_replicated).toLocaleString()}</td>
                      <td className="cell-mono cell-muted">{formatUtc(m.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function compact(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(1)}k`;
  return n.toLocaleString();
}
