import type { MigrationRecord } from "@/api/types";
import StatCard from "@/components/dashboard/StatCard";
import { formatUtc } from "@/utils/time";

export default function FallbackPanel({ record }: { record: MigrationRecord }) {
  const fb = record.fallback;
  if (!fb.active && !fb.started_at) return null;
  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">Fallback sync (Couchbase &rarr; source)</h3>
        <span style={{ display: "flex", gap: 6 }}>
          {fb.method && (
            <span className="badge badge-neutral" title={fb.method === "xdcr" ? "Reverse XDCR replication created on the destination cluster" : "META().cas polling, written back through the source's bulk API"}>
              {fb.method === "xdcr" ? "reverse XDCR" : "CAS poll + bulk write"}
            </span>
          )}
          <span className={`badge ${fb.active ? "badge-warning" : "badge-neutral"}`}>{fb.active ? "active" : "stopped"}</span>
        </span>
      </div>
      <div className="muted" style={{ marginBottom: 14 }}>
        {fb.active
          ? "Changes applications make in Couchbase are written back to the source, so you can move applications back without losing writes."
          : `Stopped${fb.stopped_at ? ` ${formatUtc(fb.stopped_at)}` : ""}.`}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <StatCard inset label="Changes written back" value={fb.changes_applied.toLocaleString()} />
        <StatCard inset label="Deletes written back" value={fb.deletes_applied.toLocaleString()} />
        <StatCard
          inset
          label={fb.method === "xdcr" ? "Changes left" : "Lag"}
          value={fb.lag_seconds != null ? (fb.method === "xdcr" ? fb.lag_seconds.toLocaleString() : `${fb.lag_seconds.toFixed(0)}s`) : "--"}
        />
        <StatCard inset label="Errors" value={fb.errors.toLocaleString()} />
      </div>
      {fb.last_error && (
        <div className="helper-banner" style={{ marginTop: 12 }}>
          Last error: {fb.last_error}
        </div>
      )}
    </div>
  );
}
