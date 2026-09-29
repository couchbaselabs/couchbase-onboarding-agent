import { useEffect, useState } from "react";
import { AlertTriangle, Undo2, X } from "lucide-react";
import { api, ApiError } from "@/api/client";
import type { MigrationRecord, RollbackOptions, RollbackPlan } from "@/api/types";

/** Shows the dry-run rollback plan for the chosen options, and requires typing
 * the migration name when the migration was already cut over. */
export default function RollbackDialog({
  record,
  onClose,
  onDone,
}: {
  record: MigrationRecord;
  onClose: () => void;
  onDone: (r: MigrationRecord) => void;
}) {
  const [options, setOptions] = useState<RollbackOptions>({
    purge_destination_data: true,
    restore_overwritten: true,
    drop_owned_collections: true,
    drop_created_bucket: false,
  });
  const [plan, setPlan] = useState<RollbackPlan | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    api.rollbackPlan(record.migration_id, options).then(setPlan).catch((e) => setError(e instanceof ApiError ? e.message : "Could not load the rollback plan."));
  }, [record.migration_id, options]);

  const needsName = !!plan?.requires_cutover_confirmation;
  const canRun = !!plan?.allowed && (!needsName || confirmText === record.plan.name) && !running;

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const updated = await api.rollbackMigration(record.migration_id, {
        ...options, reason: "user_requested", confirm_after_cutover: needsName,
      });
      onDone(updated);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Rollback failed to start.");
      setRunning(false);
    }
  }

  const toggle = (key: keyof RollbackOptions, label: string, hint: string, show = true) =>
    show && (
      <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12.5 }}>
        <input type="checkbox" style={{ marginTop: 2 }} checked={!!options[key]} onChange={(e) => setOptions({ ...options, [key]: e.target.checked })} />
        <span>
          {label}
          <div className="cb-muted">{hint}</div>
        </span>
      </label>
    );

  const isCouchbase = record.plan.source.source_type.startsWith("couchbase");

  return (
    <div className="cb-overlay" role="dialog" aria-modal="true" aria-labelledby="rollback-title" onClick={onClose}>
      <div className="cb-dialog" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <div id="rollback-title" style={{ display: "flex", gap: 8, alignItems: "center", fontWeight: 700, fontSize: 15 }}>
            <Undo2 size={17} /> Roll back "{record.plan.name}"
          </div>
          <button className="cb-btn" style={{ padding: "4px 8px" }} onClick={onClose} aria-label="Close">
            <X size={14} />
          </button>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginBottom: 16 }}>
          {toggle("drop_owned_collections", "Drop collections this migration created", "Fastest and complete for collections that didn't exist before.")}
          {toggle("purge_destination_data", "Delete migrated documents from existing collections", "Selected by the _migration tag; other documents are untouched.", !isCouchbase)}
          {toggle("restore_overwritten", "Restore documents the migration overwrote or deleted", "From the rollback journal, then drop the journal.")}
          {toggle("drop_created_bucket", "Drop the destination bucket if this migration created it", "Self-managed clusters only.", record.rollback_state.bucket_created_by_migration)}
        </div>

        {plan && (
          <>
            {plan.warnings.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginBottom: 12 }}>
                {plan.warnings.map((w, i) => (
                  <div key={i} style={{ display: "flex", gap: 8, fontSize: 12.5, color: "var(--cb-amber)" }}>
                    <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 2 }} />
                    <span>{w}</span>
                  </div>
                ))}
              </div>
            )}
            <div className="cb-section-title">What will happen</div>
            <ol style={{ margin: "0 0 14px", paddingLeft: 20, fontSize: 12.5, display: "flex", flexDirection: "column", gap: 4 }}>
              {plan.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </>
        )}

        {needsName && (
          <label style={{ display: "flex", flexDirection: "column", gap: 5, marginBottom: 14, fontSize: 12.5 }}>
            <span>
              Type the migration name <strong>{record.plan.name}</strong> to confirm a rollback after cutover:
            </span>
            <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} aria-label="Migration name" />
          </label>
        )}

        {error && <div style={{ color: "var(--cb-red-bright)", fontSize: 12.5, marginBottom: 12 }}>{error}</div>}

        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <button className="cb-btn" onClick={onClose}>
            Cancel
          </button>
          <button className="cb-btn cb-btn-primary" disabled={!canRun} onClick={run}>
            {running ? "Starting..." : "Roll back"}
          </button>
        </div>
      </div>
    </div>
  );
}
