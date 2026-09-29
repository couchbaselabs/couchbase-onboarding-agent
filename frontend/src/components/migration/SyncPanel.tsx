import { RefreshCw } from "lucide-react";
import type { MigrationRecord, SyncStage } from "@/api/types";
import StatCard from "@/components/dashboard/StatCard";
import { CDC_METHOD_LABELS } from "@/theme/tokens";
import { ago } from "@/utils/time";

const STAGES: { key: SyncStage; label: string }[] = [
  { key: "capturing", label: "Record position" },
  { key: "bulk_copy", label: "Bulk copy" },
  { key: "catch_up", label: "Catch up" },
  { key: "streaming", label: "Streaming" },
  { key: "draining", label: "Cutover" },
];

const ORDER: Record<string, number> = { capturing: 0, bulk_copy: 1, catch_up: 2, recovering: 2, streaming: 3, draining: 4 };

export default function SyncPanel({
  record,
  busy,
  onReconcile,
}: {
  record: MigrationRecord;
  busy: boolean;
  onReconcile: () => void;
}) {
  const { sync, stats, plan } = record;
  const hasBulk = plan.strategy === "full_load_and_cdc";
  const cutOver = !!record.cutover_at;
  const current = cutOver ? 5 : ORDER[sync.stage] ?? -1;
  const canReconcile = record.phase === "replicating" || record.phase === "stopped";
  const stages = STAGES.filter((s) => hasBulk || (s.key !== "capturing" && s.key !== "bulk_copy"));
  const lag = stats.replication_lag_seconds;
  const rec = sync.reconcile;

  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">Continuous sync</h3>
        {sync.cdc_method && <span className="tag-chip">{CDC_METHOD_LABELS[sync.cdc_method] ?? sync.cdc_method}</span>}
      </div>

      <div className="cb-stages" style={{ marginBottom: 14 }}>
        {stages.map((s) => {
          const idx = ORDER[s.key];
          let state = idx < current ? "done" : idx === current ? "active" : "pending";
          if (s.key === "catch_up" && sync.stage === "recovering") state = "warn";
          if (!cutOver && sync.stage === "stopped" && idx <= 3 && sync.caught_up_at) state = "done";
          return (
            <div key={s.key} className="cb-stage" data-state={state} aria-current={state === "active" ? "step" : undefined}>
              {s.key === "catch_up" && sync.stage === "recovering" ? "Recovering" : s.label}
            </div>
          );
        })}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 14 }}>
        <StatCard inset label="Changes applied" value={stats.mutations_replicated.toLocaleString()} />
        <StatCard inset label="Changes/sec" value={stats.mutations_per_sec.toFixed(1)} />
        <StatCard inset label="Replication lag" value={lag != null ? `${lag.toFixed(0)}s` : "--"} />
        <StatCard
          inset
          label="Checkpoint saved"
          value={ago(sync.last_checkpoint_at)}
          sub={sync.stream_restarts ? `${sync.stream_restarts} stream restart(s)` : undefined}
        />
      </div>

      {sync.last_stream_error && sync.stage === "recovering" && (
        <div className="helper-banner" style={{ marginBottom: 12 }}>
          Retrying after: {sync.last_stream_error}
        </div>
      )}

      <div
        style={{
          display: "flex", alignItems: "center", gap: 12, borderTop: "1px solid var(--border-soft)",
          paddingTop: 14, flexWrap: "wrap",
        }}
      >
        <div style={{ flex: "1 1 320px", fontSize: 13 }}>
          <div style={{ fontWeight: 600, marginBottom: 3 }}>Delete reconciliation</div>
          <div className="muted">
            {plan.reconcile_interval_minutes
              ? `Runs every ${plan.reconcile_interval_minutes} min while streaming.`
              : "Manual only."}{" "}
            {rec.last_finished_at ? `Last run ${ago(rec.last_finished_at)}: ${rec.last_result ?? ""}.` : "Not run yet."}
            {rec.deletes_applied_total > 0 && ` ${rec.deletes_applied_total.toLocaleString()} delete(s) applied in total.`}
          </div>
        </div>
        {canReconcile && (
          <button className="btn btn-secondary btn-sm" disabled={busy || rec.running} onClick={onReconcile}>
            <RefreshCw size={13} className={rec.running ? "cb-spin" : undefined} /> {rec.running ? "Reconciling..." : "Reconcile now"}
          </button>
        )}
      </div>
    </div>
  );
}
