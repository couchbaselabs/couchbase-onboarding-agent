import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, Wrench } from "lucide-react";
import { api, ApiError } from "@/api/client";
import type { MigrationRecord } from "@/api/types";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import StatCard from "@/components/dashboard/StatCard";
import ThroughputChart from "@/components/dashboard/ThroughputChart";
import TopologyDiagram from "@/components/topology/TopologyDiagram";
import SyncPanel from "@/components/migration/SyncPanel";
import RollbackDialog from "@/components/migration/RollbackDialog";
import VerificationTable from "@/components/migration/VerificationTable";
import FallbackPanel from "@/components/migration/FallbackPanel";
import { phaseLabel, statusColor, SOURCE_TYPE_LABELS, tint } from "@/theme/tokens";
import { formatUtc } from "@/utils/time";

const STREAMING_PHASES = new Set(["replicating", "fallback_syncing"]);
const ROLLBACK_PHASES = new Set(["approved", "migrating", "replicating", "verifying", "complete", "failed", "stopped", "cancelled", "fell_back"]);

export default function MigrationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [initial, setInitial] = useState<MigrationRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showRollback, setShowRollback] = useState(false);
  const [confirmFallBack, setConfirmFallBack] = useState(false);
  const { record: live } = useMigrationSocket(id || "*");

  useEffect(() => {
    if (!id) return;
    api.getMigration(id).then(setInitial).catch((err) => setActionError(err instanceof ApiError ? err.message : "Could not load the migration."));
  }, [id]);

  const record =
    live && live.migration_id === id && (!initial || live.updated_at >= initial.updated_at) ? live : initial;

  if (!record) {
    return <div style={{ padding: 32, color: "var(--text-muted)" }}>{actionError ?? "Loading..."}</div>;
  }

  async function run(action: () => Promise<MigrationRecord>) {
    setBusy(true);
    setActionError(null);
    try {
      setInitial(await action());
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  const { plan, stats, phase, sync } = record;
  const mid = record.migration_id;
  const continuous = plan.strategy !== "full_load";
  const streaming = STREAMING_PHASES.has(phase);
  const draining = sync.stage === "draining";
  const canResume = continuous && (phase === "failed" || phase === "stopped") && !record.cutover_at && !!record.approved_at;
  const canFallback = phase === "complete" && !!record.cutover_at;

  return (
    <div style={{ padding: 32, maxWidth: 1100 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20, gap: 12 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>{plan.name}</h1>
          <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 4 }}>
            {SOURCE_TYPE_LABELS[plan.source.source_type]} ({plan.source.label}) &rarr; {plan.destination.label} /{" "}
            {plan.destination_bucket}
            {record.cutover_at && <> &middot; cut over {formatUtc(record.cutover_at)}</>}
          </div>
        </div>
        <span className="cb-badge" style={{ background: tint(statusColor(phase)), color: statusColor(phase), fontSize: 12 }}>
          {draining ? "cutting over" : phaseLabel(phase)}
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
          destNodes={record.validation_report?.dest_topology?.nodes.map((n) => ({ hostname: n.hostname, services: n.services }))}
          phase={phase}
          throughputPerSec={streaming ? stats.mutations_per_sec : stats.throughput_docs_per_sec}
        />
      </div>

      {record.error_message && (
        <div className="cb-card" style={{ marginBottom: 20, borderColor: "var(--cb-red-bright)", color: "var(--cb-red-bright)" }}>
          {record.error_message}
        </div>
      )}
      {record.rollback_state.last_summary && phase === "rolled_back" && (
        <div className="cb-card" style={{ marginBottom: 20, borderColor: "var(--cb-green)" }}>{record.rollback_state.last_summary}</div>
      )}
      {actionError && (
        <div className="cb-card" role="alert" style={{ marginBottom: 20, borderColor: "var(--cb-red-bright)", color: "var(--cb-red-bright)" }}>
          {actionError}
        </div>
      )}

      <div className="cb-card" style={{ marginBottom: 20 }}>
        <div className="cb-section-title">Actions</div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {phase === "approved" && (
            <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => run(() => api.startMigration(mid))}>
              {continuous ? "Start migration + sync" : "Start migration"}
            </button>
          )}
          {phase === "replicating" && !draining && (
            <>
              <button
                className="cb-btn cb-btn-primary"
                disabled={busy}
                title="Stop application writes to the source first. The remaining changes are applied, counts are verified, then Couchbase becomes the system of record."
                onClick={() => run(() => api.stopReplication(mid, true))}
              >
                Cut over
              </button>
              <button className="cb-btn" disabled={busy} onClick={() => run(() => api.stopReplication(mid, false))}>
                Pause sync
              </button>
            </>
          )}
          {canResume && (
            <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => run(() => api.resumeMigration(mid))}>
              Resume from checkpoint
            </button>
          )}
          {canFallback && (
            <button
              className="cb-btn"
              disabled={busy}
              title="Write changes made in Couchbase since cutover back to the source, so applications can move back without losing data."
              onClick={() => run(() => api.startFallback(mid))}
            >
              Start fallback sync
            </button>
          )}
          {phase === "fallback_syncing" && (
            <>
              {!confirmFallBack ? (
                <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => setConfirmFallBack(true)}>
                  Fall back to source...
                </button>
              ) : (
                <button className="cb-btn cb-btn-primary" disabled={busy} onClick={() => run(() => api.stopFallback(mid, true))}>
                  Confirm: writes to Couchbase have stopped -- fall back now
                </button>
              )}
              <button className="cb-btn" disabled={busy} onClick={() => run(() => api.stopFallback(mid, false))}>
                Stop fallback sync (stay on Couchbase)
              </button>
            </>
          )}
          {ROLLBACK_PHASES.has(phase) && (
            <button className="cb-btn cb-btn-danger" disabled={busy} onClick={() => setShowRollback(true)}>
              Roll back...
            </button>
          )}
        </div>
        {draining && (
          <div className="cb-muted" style={{ marginTop: 10 }}>
            Cutover in progress: applying the remaining changes, then verifying counts.
          </div>
        )}
      </div>

      {continuous && (streaming || phase === "migrating" || sync.stage !== "idle") && phase !== "fallback_syncing" && (
        <SyncPanel record={record} busy={busy} onReconcile={() => run(() => api.reconcile(mid))} />
      )}

      <FallbackPanel record={record} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 14, marginBottom: 20 }}>
        <StatCard label="Bulk copy: docs" value={stats.docs_migrated.toLocaleString()} sub={stats.docs_total ? `of ~${stats.docs_total.toLocaleString()}` : undefined} />
        <StatCard label="Failed writes" value={stats.docs_failed.toLocaleString()} />
        <StatCard label="Throughput" value={`${stats.throughput_docs_per_sec.toFixed(1)}/s`} />
        <StatCard label="Rollback journal" value={record.rollback_state.journaled_docs.toLocaleString()} sub="pre-images saved" />
      </div>

      <div style={{ marginBottom: 20 }}>
        <ThroughputChart value={streaming ? stats.mutations_per_sec : stats.throughput_docs_per_sec} label={streaming ? "Changes/sec" : "Docs/sec"} />
      </div>

      <VerificationTable record={record} />

      {record.failed_events.length > 0 && (
        <div className="cb-card" style={{ marginBottom: 20 }}>
          <div className="cb-section-title">Writes that failed after retries ({record.failed_events.length})</div>
          <div className="cb-muted" style={{ marginBottom: 8 }}>
            These didn't block the migration. Reconciliation or re-running the bulk copy repairs them once the cause is fixed.
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="cb-table">
              <thead>
                <tr>
                  <th>Container</th>
                  <th>Key</th>
                  <th>Op</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {record.failed_events.slice(-10).reverse().map((e, i) => (
                  <tr key={i}>
                    <td>{e.container}</td>
                    <td style={{ fontFamily: "ui-monospace, monospace" }}>{e.key}</td>
                    <td>{e.op}</td>
                    <td style={{ color: "var(--text-secondary)" }}>{e.error}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {record.bottleneck_findings.length > 0 && (
        <div className="cb-card" style={{ marginBottom: 20 }}>
          <div className="cb-section-title">Bottleneck findings</div>
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

      <div className="cb-card">
        <div className="cb-section-title">Log</div>
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

      {showRollback && (
        <RollbackDialog
          record={record}
          onClose={() => setShowRollback(false)}
          onDone={(r) => {
            setInitial(r);
            setShowRollback(false);
          }}
        />
      )}
    </div>
  );
}
