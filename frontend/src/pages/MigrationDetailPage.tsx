import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, CheckCircle2, Loader2, Wrench, XCircle } from "lucide-react";
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
import { phaseLabel, phaseTone, SOURCE_TYPE_LABELS } from "@/theme/tokens";
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
    return actionError ? (
      <div className="helper-banner helper-banner-danger" role="alert">
        <XCircle size={15} />
        <span>{actionError}</span>
      </div>
    ) : (
      <div className="loading-note">Loading...</div>
    );
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
  const canFailback = phase === "fell_back" && continuous;
  const targetBuckets = plan.target_buckets?.length ? plan.target_buckets : [plan.destination_bucket];

  return (
    <>
      <Link to="/" className="back-link">
        <ArrowLeft size={13} /> Migrations
      </Link>
      <div className="page-header">
        <div style={{ minWidth: 0 }}>
          <h1 className="page-title">
            {plan.name}
            <span className={`badge badge-lg badge-${draining ? "info" : phaseTone(phase)}`}>
              {draining ? "cutting over" : phaseLabel(phase)}
            </span>
          </h1>
          <p className="page-subtitle">
            {SOURCE_TYPE_LABELS[plan.source.source_type]} ({plan.source.label}) &rarr; {plan.destination.label} /{" "}
            {targetBuckets.join(", ")}
            {record.cutover_at && <> &middot; cut over {formatUtc(record.cutover_at)}</>}
            {!!record.failbacks && <> &middot; {record.failbacks} failback{record.failbacks === 1 ? "" : "s"}</>}
          </p>
        </div>
        <div className="header-actions">
          {phase === "approved" && (
            <button className="btn btn-primary" disabled={busy} onClick={() => run(() => api.startMigration(mid))}>
              {continuous ? "Start migration + sync" : "Start migration"}
            </button>
          )}
          {phase === "replicating" && !draining && (
            <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => run(() => api.stopReplication(mid, false))}>
                Pause sync
              </button>
              <button
                className="btn btn-primary"
                disabled={busy}
                title="Stop application writes to the source first. The remaining changes are applied, counts are verified, then Couchbase becomes the system of record."
                onClick={() => run(() => api.stopReplication(mid, true))}
              >
                Cut over
              </button>
            </>
          )}
          {canResume && (
            <button className="btn btn-primary" disabled={busy} onClick={() => run(() => api.resumeMigration(mid))}>
              Resume from checkpoint
            </button>
          )}
          {canFallback && (
            <button
              className="btn btn-secondary"
              disabled={busy}
              title="Write changes made in Couchbase since cutover back to the source, so applications can move back without losing data."
              onClick={() => run(() => api.startFallback(mid))}
            >
              Start fallback sync
            </button>
          )}
          {phase === "fallback_syncing" && (
            <>
              <button className="btn btn-secondary" disabled={busy} onClick={() => run(() => api.stopFallback(mid, false))}>
                Stop fallback sync (stay on Couchbase)
              </button>
              {!confirmFallBack ? (
                <button className="btn btn-primary" disabled={busy} onClick={() => setConfirmFallBack(true)}>
                  Fall back to source...
                </button>
              ) : (
                <button className="btn btn-primary btn-wrap" disabled={busy} onClick={() => run(() => api.stopFallback(mid, true))}>
                  Confirm: writes to Couchbase have stopped -- fall back now
                </button>
              )}
            </>
          )}
          {canFailback && (
            <button
              className="btn btn-primary"
              disabled={busy}
              title="Resume forward sync from the position recorded when applications fell back. Couchbase catches up on everything written to the source since; cut over again when it's caught up."
              onClick={() => run(() => api.failback(mid))}
            >
              Return to Couchbase (resume sync)
            </button>
          )}
          {ROLLBACK_PHASES.has(phase) && (
            <button className="btn btn-danger-outline" disabled={busy} onClick={() => setShowRollback(true)}>
              Roll back...
            </button>
          )}
        </div>
      </div>

      <div className="stack">
        {draining && (
          <div className="helper-banner helper-banner-neutral" role="status">
            <Loader2 size={14} className="cb-spin" />
            <span>Cutover in progress: applying the remaining changes, then verifying counts.</span>
          </div>
        )}
        {phase === "fell_back" && (
          <div className="helper-banner helper-banner-neutral" role="status">
            <CheckCircle2 size={14} />
            <span>
              The source is the system of record again.
              {continuous
                ? " Forward sync can resume from the position recorded at fall-back time, so moving back to Couchbase later doesn't need another bulk copy."
                : ""}
            </span>
          </div>
        )}
        {record.error_message && (
          <div className="helper-banner helper-banner-danger" role="alert">
            <XCircle size={15} />
            <span>{record.error_message}</span>
          </div>
        )}
        {record.rollback_state.last_summary && phase === "rolled_back" && (
          <div className="helper-banner helper-banner-success">
            <CheckCircle2 size={15} />
            <span>{record.rollback_state.last_summary}</span>
          </div>
        )}
        {actionError && (
          <div className="helper-banner helper-banner-danger" role="alert">
            <XCircle size={15} />
            <span>{actionError}</span>
          </div>
        )}

        <div className="stat-grid" style={{ marginBottom: 0 }}>
          <StatCard
            label="Bulk copy: docs"
            value={stats.docs_migrated.toLocaleString()}
            sub={stats.docs_total ? `of ~${stats.docs_total.toLocaleString()}` : "documents written"}
          />
          <StatCard
            label="Failed writes"
            value={stats.docs_failed.toLocaleString()}
            sub={stats.docs_failed ? "retried, then set aside" : "none"}
            valueColor={stats.docs_failed ? "var(--red-dim)" : undefined}
          />
          <StatCard
            label={streaming ? "Changes/sec" : "Throughput"}
            value={`${(streaming ? stats.mutations_per_sec : stats.throughput_docs_per_sec).toFixed(1)}/s`}
            sub={streaming ? "continuous sync" : "bulk copy"}
          />
          <StatCard label="Rollback journal" value={record.rollback_state.journaled_docs.toLocaleString()} sub="pre-images saved" />
        </div>

        <div className="card">
          <h3 className="card-title">Data flow</h3>
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

        {continuous && (streaming || phase === "migrating" || sync.stage !== "idle") && phase !== "fallback_syncing" && (
          <SyncPanel record={record} busy={busy} onReconcile={() => run(() => api.reconcile(mid))} />
        )}

        <FallbackPanel record={record} />

        <ThroughputChart value={streaming ? stats.mutations_per_sec : stats.throughput_docs_per_sec} label={streaming ? "Changes/sec" : "Docs/sec"} />

        <VerificationTable record={record} />

        {record.failed_events.length > 0 && (
          <div className="card">
            <div className="card-title-row">
              <h3 className="card-title">Writes that failed after retries</h3>
              <span className="badge badge-danger">{record.failed_events.length}</span>
            </div>
            <div className="muted" style={{ marginBottom: 10 }}>
              These didn't block the migration. Reconciliation or re-running the bulk copy repairs them once the cause is fixed.
            </div>
            <div className="table-wrap">
              <table className="data-table">
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
                      <td className="cell-mono">{e.key}</td>
                      <td>
                        <span className="badge badge-neutral">{e.op}</span>
                      </td>
                      <td className="cell-muted">{e.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {record.bottleneck_findings.length > 0 && (
          <div className="card">
            <h3 className="card-title">Bottleneck findings</h3>
            <div className="stack" style={{ gap: 10 }}>
              {record.bottleneck_findings.slice(-5).reverse().map((f) => (
                <div key={f.finding_id} className={`finding-card ${f.auto_remediated ? "sev-pass" : "sev-warning"}`}>
                  {f.auto_remediated ? (
                    <Wrench size={15} color="var(--teal)" style={{ flexShrink: 0, marginTop: 1 }} aria-label="Auto-remediated" />
                  ) : (
                    <AlertTriangle size={15} color="var(--amber)" style={{ flexShrink: 0, marginTop: 1 }} aria-label="Needs attention" />
                  )}
                  <div>
                    <div className="finding-title">{f.message}</div>
                    <p className="finding-summary">{f.suggestion}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="card">
          <h3 className="card-title">Log</h3>
          <div className="console cb-scrollbar">
            {record.log_tail.slice().reverse().map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
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
    </>
  );
}
