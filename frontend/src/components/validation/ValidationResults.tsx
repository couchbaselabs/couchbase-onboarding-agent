import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type { ValidationReport } from "@/api/types";

/** Pre-flight checks, one card each, severity on the left edge (the Agent
 * Operations Manager's finding-card layout). */
export default function ValidationResults({ report }: { report: ValidationReport }) {
  const passed = report.checks.filter((c) => c.passed).length;
  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className="section-row" style={{ marginBottom: 0 }}>
        <h2 className="section-heading">Pre-flight checks</h2>
        <span className={`badge ${report.passed ? "badge-success" : "badge-danger"}`}>
          {passed} of {report.checks.length} passed
        </span>
      </div>
      {report.checks.map((c) => {
        const sev = c.passed ? "pass" : c.severity === "error" ? "error" : c.severity === "warning" ? "warning" : "info";
        return (
          <div key={c.check_id} className={`finding-card sev-${sev}`}>
            <div style={{ marginTop: 1 }}>
              {sev === "pass" ? (
                <CheckCircle2 size={16} color="var(--green)" aria-label="Passed" />
              ) : sev === "error" ? (
                <XCircle size={16} color="var(--red-dim)" aria-label="Failed" />
              ) : sev === "warning" ? (
                <AlertTriangle size={16} color="var(--amber)" aria-label="Warning" />
              ) : (
                <Info size={16} color="var(--blue)" aria-label="Note" />
              )}
            </div>
            <div>
              <div className="finding-title">{c.label}</div>
              <p className="finding-summary">{c.message}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
