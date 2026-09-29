import type { MigrationRecord } from "@/api/types";
import StatCard from "@/components/dashboard/StatCard";
import { formatUtc } from "@/utils/time";

export default function FallbackPanel({ record }: { record: MigrationRecord }) {
  const fb = record.fallback;
  if (!fb.active && !fb.started_at) return null;
  return (
    <div className="cb-card" style={{ marginBottom: 20 }}>
      <div className="cb-section-title">Fallback sync (Couchbase &rarr; source)</div>
      <div className="cb-muted" style={{ marginBottom: 12 }}>
        {fb.active
          ? "Changes applications make in Couchbase are written back to the source, so you can move applications back without losing writes."
          : `Stopped${fb.stopped_at ? ` ${formatUtc(fb.stopped_at)}` : ""}.`}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <StatCard label="Changes written back" value={fb.changes_applied.toLocaleString()} />
        <StatCard label="Deletes written back" value={fb.deletes_applied.toLocaleString()} />
        <StatCard label="Lag" value={fb.lag_seconds != null ? `${fb.lag_seconds.toFixed(0)}s` : "--"} />
        <StatCard label="Errors" value={fb.errors.toLocaleString()} />
      </div>
      {fb.last_error && (
        <div style={{ fontSize: 12.5, color: "var(--cb-amber)", marginTop: 10 }}>Last error: {fb.last_error}</div>
      )}
    </div>
  );
}
