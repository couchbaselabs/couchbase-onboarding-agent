import { CheckCircle2, MinusCircle } from "lucide-react";
import type { SourceTopologySnapshot } from "@/api/types";
import { CDC_METHOD_LABELS } from "@/theme/tokens";

/** What continuous sync gives you on the introspected source, stated plainly. */
export default function CdcCapabilities({ topology }: { topology: SourceTopologySnapshot }) {
  const rows: { ok: boolean; label: string; detail: string }[] = [
    {
      ok: topology.supports_cdc,
      label: topology.cdc_method ? CDC_METHOD_LABELS[topology.cdc_method] ?? topology.cdc_method : "Change capture",
      detail: topology.supports_cdc ? "Continuous sync available" : "Continuous sync not available yet",
    },
    {
      ok: !!topology.cdc_resumable,
      label: "Resumable",
      detail: topology.cdc_resumable
        ? "Restarts continue from a checkpoint"
        : "Restarts re-scan the source (notifications aren't durable)",
    },
    {
      ok: !!topology.cdc_captures_deletes,
      label: "Deletes",
      detail: topology.cdc_captures_deletes ? "Arrive through the change stream" : "Applied by periodic reconciliation",
    },
    {
      ok: !!topology.reverse_sync_supported,
      label: "Fallback after cutover",
      detail: topology.reverse_sync_supported ? "Couchbase changes can be written back" : "Not supported",
    },
  ];
  return (
    <div className="cb-card" style={{ padding: 14 }}>
      <div className="cb-section-title">Continuous sync on this source</div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
        {rows.map((r) => (
          <div key={r.label} style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 12.5 }}>
            {r.ok ? (
              <CheckCircle2 size={15} color="var(--cb-green)" style={{ flexShrink: 0, marginTop: 1 }} />
            ) : (
              <MinusCircle size={15} color="var(--text-muted)" style={{ flexShrink: 0, marginTop: 1 }} />
            )}
            <div>
              <div style={{ fontWeight: 600 }}>{r.label}</div>
              <div className="cb-muted">{r.detail}</div>
            </div>
          </div>
        ))}
      </div>
      {topology.cdc_notes && <div className="cb-muted" style={{ marginTop: 10 }}>{topology.cdc_notes}</div>}
    </div>
  );
}
