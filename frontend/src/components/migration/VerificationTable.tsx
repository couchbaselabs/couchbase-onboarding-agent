import { CheckCircle2, HelpCircle, XCircle } from "lucide-react";
import type { MigrationRecord } from "@/api/types";
import { formatUtc } from "@/utils/time";

export default function VerificationTable({ record }: { record: MigrationRecord }) {
  if (!record.verification.length) return null;
  const matched = record.verification.filter((v) => v.matched === true).length;
  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">
          Verification
          {record.verified_at && (
            <span className="cell-muted" style={{ fontWeight: 400, fontSize: 12.5 }}>
              {" "}
              &middot; {formatUtc(record.verified_at)}
            </span>
          )}
        </h3>
        <span className={`badge ${matched === record.verification.length ? "badge-success" : "badge-warning"}`}>
          {matched} of {record.verification.length} match
        </span>
      </div>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Container</th>
              <th>Destination</th>
              <th className="num">Source</th>
              <th className="num">Couchbase</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {record.verification.map((v) => (
              <tr key={v.container}>
                <td style={{ fontWeight: 600 }}>{v.container}</td>
                <td className="cell-mono cell-muted">{v.target}</td>
                <td className="num">{v.source_count?.toLocaleString() ?? "--"}</td>
                <td className="num">{v.dest_count?.toLocaleString() ?? "--"}</td>
                <td title={v.note ?? undefined}>
                  {v.matched === true ? (
                    <span className="badge badge-success">
                      <CheckCircle2 size={12} aria-hidden="true" /> match
                    </span>
                  ) : v.matched === false ? (
                    <span className="badge badge-danger">
                      <XCircle size={12} aria-hidden="true" /> differ
                    </span>
                  ) : (
                    <span className="badge badge-neutral">
                      <HelpCircle size={12} aria-hidden="true" /> not compared
                    </span>
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
