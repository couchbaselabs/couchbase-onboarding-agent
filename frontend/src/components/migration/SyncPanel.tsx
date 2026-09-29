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
    <div className="cb-card" style={{ marginBottom: 20 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, gap: 12 }}>
        <div className="cb-section-title" style={{ marginBottom: 0 }}>Continuous sync</div>
        {sync.cdc_method && <span className="cb-pill">{CDC_METHOD_LABELS[sync.cdc_method] ?? sync.cdc_method}</span>}
      </div>

      <div className="cb-stages" style={{ marginBottom: 14 }}>
        {stages.map((s) => {
          const idx = ORDER[s.key];
          let state = idx < current ? "done" : idx === current ? "active" : "pending";
          if (s.key === "catch_up" && sync.stage === "recovering") state = "warn";
          if (!cutOver && sync.stage === "stopped" && idx <= 3 && sync.caught_up_at) state = "done";
          return (
            <div key={s.key} className="cb-stage" data-state={state}>
              {s.key === "catch_up" && sync.stage === "recovering" ? "Recovering" : s.label}
            </div>
          );
        })}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 12 }}>
        <StatCard label="Changes applied" value={stats.mutations_replicated.toLocaleString()} />
        <StatCard label="Changes/sec" value={stats.mutations_per_sec.toFixed(1)} />
        <StatCard label="Replication lag" value={lag != null ? `${lag.toFixed(0)}s` : "--"} />
        <StatCard
          label="Checkpoint saved"
          value={ago(sync.last_checkpoint_at)}
          sub={sync.stream_restarts ? `${sync.stream_restarts} stream restart(s)` : undefined}
        />
      </div>

      {sync.last_stream_error && sync.stage === "recovering" && (
        <div style={{ fontSize: 12.5, color: "var(--cb-amber)", marginBottom: 10 }}>
          Retrying after: {sync.last_stream_error}
        </div>
      )}

      <div
        style={{
          display: "flex", alignItems: "flex-start", gap: 12, borderTop: "1px solid var(--border-subtle)",
          paddingTop: 12, flexWrap: "wrap",
        }}
      >
        <div style={{ flex: "1 1 320px", fontSize: 12.5 }}>
          <div style={{ fontWeight: 600, marginBottom: 3 }}>Delete reconciliation</div>
          <div className="cb-muted">
            {plan.reconcile_interval_minutes
              ? `Runs every ${plan.reconcile_interval_minutes} min while streaming.`
              : "Manual only."}{" "}
            {rec.last_finished_at ? `Last run ${ago(rec.last_finished_at)}: ${rec.last_result ?? ""}.` : "Not run yet."}
            {rec.deletes_applied_total > 0 && ` ${rec.deletes_applied_total.toLocaleString()} delete(s) applied in total.`}
          </div>
        </div>
        {canReconcile && (
          <button className="cb-btn" disabled={busy || rec.running} onClick={onReconcile}>
            <RefreshCw size={13} className={rec.running ? "cb-spin" : undefined} /> {rec.running ? "Reconciling..." : "Reconcile now"}
          </button>
        )}
      </div>
    </div>
  );
}
