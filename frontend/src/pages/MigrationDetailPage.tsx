import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, Wrench } from "lucide-react";
import { api, ApiError } from "@/api/client";
import type { MigrationRecord } from "@/api/types";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import StatCard from "@/components/dashboard/StatCard";
import ThroughputChart from "@/components/dashboard/ThroughputChart";
import TopologyDiagram from "@/components/topology/TopologyDiagram";
import { statusColor, SOURCE_TYPE_LABELS } from "@/theme/tokens";

const CONTINUOUS_PHASES = new Set(["replicating"]);

export default function MigrationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [initial, setInitial] = useState<MigrationRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const { record: live } = useMigrationSocket(id || "*");

  useEffect(() => {
    if (!id) return;
    api.getMigration(id).then(setInitial);
  }, [id]);

  const record = live && live.migration_id === id ? live : initial;

  if (!record) {
    return <div style={{ padding: 32, color: "var(--text-muted)" }}>Loading...</div>;
  }

  async function run(action: () => Promise<MigrationRecord>) {
    setBusy(true);
    setActionError(null);
    try {
      const updated = await action();
      setInitial(updated);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  const { plan, stats, phase } = record;

  return (
    <div style={{ padding: 32, maxWidth: 1100 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>{plan.name}</h1>
          <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 4 }}>
            {SOURCE_TYPE_LABELS[plan.source.source_type]} ({plan.source.label}) &rarr; {plan.destination.label} /{" "}
            {plan.destination_bucket}
          </div>
        </div>
        <span className="cb-badge" style={{ background: `${statusColor(phase)}22`, color: statusColor(phase), fontSize: 12 }}>
          {phase.replace(/_/g, " ")}
        </span>
      </div>

      <div className="cb-card" style={{ marginBottom: 20 }}>
        <TopologyDiagram
          sourceType={plan.source.source_type}
          sourceLabel={plan.source.label}
          containerCount={plan.containers.filter((c) => c.include).length}
          destLabel={plan.destination.label}
          isCapella={plan.destination.is_capella}
          bucket={plan.destination_bucket}
          bucketCount={record.validation_report?.dest_topology?.buckets.length || 1}
          destNodes={record.validation_report?.dest_topology?.nodes.map((n) => ({
            hostname: n.hostname,
            services: n.services,
          }))}
          phase={phase}
          throughputPerSec={CONTINUOUS_PHASES.has(phase) ? stats.mutations_per_sec : stats.throughput_docs_per_sec}
        />
      </div>

      {record.error_message && (
        <div className="cb-card" style={{ marginBottom: 20, borderColor: "var(--cb-red-bright)", color: "var(--cb-red-bright)" }}>
          {record.error_message}
        </div>
      )}
      {actionError && (
        <div className="cb-card" style={{ marginBottom: 20, borderColor: "var(--cb-red-bright)", color: "var(--cb-red-bright)" }}>
          {actionError}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 14, marginBottom: 20 }}>
        <StatCard label="Docs migrated" value={stats.docs_migrated.toLocaleString()} sub={`of ${stats.docs_total.toLocaleString() || "?"}`} />
        <StatCard label="Docs failed" value={stats.docs_failed.toLocaleString()} />
        <StatCard label="Throughput" value={`${stats.throughput_docs_per_sec.toFixed(1)}/s`} />
        <StatCard label="Error rate" value={`${stats.error_rate_pct.toFixed(1)}%`} />
      </div>

      {CONTINUOUS_PHASES.has(phase) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14, marginBottom: 20 }}>
          <StatCard label="Mutations replicated" value={stats.mutations_replicated.toLocaleString()} />
          <StatCard label="Mutations/sec" value={stats.mutations_per_sec.toFixed(1)} />
          <StatCard
            label="Replication lag"
            value={stats.replication_lag_seconds != null ? `${stats.replication_lag_seconds.toFixed(0)}s` : "--"}
          />
        </div>
      )}

      <div style={{ marginBottom: 20 }}>
        <ThroughputChart value={CONTINUOUS_PHASES.has(phase) ? stats.mutations_per_sec : stats.throughput_docs_per_sec} label="Docs/sec" />
      </div>

      {record.bottleneck_findings.length > 0 && (
        <div className="cb-card" style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Bottleneck findings</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {record.bottleneck_findings.slice(-5).reverse().map((f) => (
              <div key={f.finding_id} style={{ display: "flex", gap: 8, fontSize: 12.5 }}>
                {f.auto_remediated ? (
                  <Wrench size={14} color="var(--cb-teal)" style={{ flexShrink: 0, marginTop: 1 }} />
                ) : (
                  <AlertTriangle size={14} color="var(--cb-amber)" style={{ flexShrink: 0, marginTop: 1 }} />
                )}
                <div>
                  <div>{f.message}</div>
                  <div style={{ color: "var(--text-muted)" }}>{f.suggestion}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="cb-card" style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Actions</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {phase === "approved" && (
            <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => run(() => api.startMigration(record.migration_id))}>
              {plan.is_continuous ? "Start replication" : "Start migration"}
            </button>
          )}
          {phase === "replicating" && (
            <>
              <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => run(() => api.stopReplication(record.migration_id, true))}>
                Cutover &amp; complete
              </button>
              <button className="cb-btn" disabled={busy} onClick={() => run(() => api.stopReplication(record.migration_id, false))}>
                Stop replication
              </button>
            </>
          )}
          {["migrating", "replicating", "verifying", "complete", "failed", "stopped"].includes(phase) && (
            <button
              className="cb-btn"
              disabled={busy}
              onClick={() => run(() => api.rollbackMigration(record.migration_id, "user_requested", true))}
            >
              Roll back (purge destination data)
            </button>
          )}
        </div>
      </div>

      <div className="cb-card">
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Log</div>
        <div
          className="cb-scrollbar"
          style={{
            maxHeight: 260, overflowY: "auto", fontFamily: "ui-monospace, monospace", fontSize: 11.5,
            color: "var(--text-secondary)", display: "flex", flexDirection: "column", gap: 3,
          }}
        >
          {record.log_tail.slice().reverse().map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      </div>
    </div>
  );
}
