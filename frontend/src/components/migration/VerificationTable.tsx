import { CheckCircle2, HelpCircle, XCircle } from "lucide-react";
import type { MigrationRecord } from "@/api/types";
import { formatUtc } from "@/utils/time";

export default function VerificationTable({ record }: { record: MigrationRecord }) {
  if (!record.verification.length) return null;
  return (
    <div className="cb-card" style={{ marginBottom: 20 }}>
      <div className="cb-section-title">
        Verification{record.verified_at ? ` (${formatUtc(record.verified_at)})` : ""}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="cb-table">
          <thead>
            <tr>
              <th>Container</th>
              <th>Destination</th>
              <th style={{ textAlign: "right" }}>Source</th>
              <th style={{ textAlign: "right" }}>Couchbase</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {record.verification.map((v) => (
              <tr key={v.container}>
                <td style={{ fontWeight: 600 }}>{v.container}</td>
                <td style={{ color: "var(--text-secondary)" }}>{v.target}</td>
                <td style={{ textAlign: "right" }}>{v.source_count?.toLocaleString() ?? "--"}</td>
                <td style={{ textAlign: "right" }}>{v.dest_count?.toLocaleString() ?? "--"}</td>
                <td title={v.note ?? undefined}>
                  {v.matched === true ? (
                    <CheckCircle2 size={15} color="var(--cb-green)" aria-label="Counts match" />
                  ) : v.matched === false ? (
                    <XCircle size={15} color="var(--cb-red-bright)" aria-label="Counts differ" />
                  ) : (
                    <HelpCircle size={15} color="var(--text-muted)" aria-label="Not compared" />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
