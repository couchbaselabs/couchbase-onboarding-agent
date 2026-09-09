import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type { ValidationReport } from "@/api/types";

export default function ValidationResults({ report }: { report: ValidationReport }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {report.checks.map((c) => (
        <div
          key={c.check_id}
          className="cb-card"
          style={{
            padding: "12px 14px",
            display: "flex",
            gap: 12,
            alignItems: "flex-start",
            borderColor: c.passed
              ? "var(--border-subtle)"
              : c.severity === "error"
                ? "var(--cb-red-bright)"
                : "var(--cb-amber)",
          }}
        >
          <div style={{ marginTop: 1 }}>
            {c.passed ? (
              <CheckCircle2 size={16} color="var(--cb-green)" />
            ) : c.severity === "error" ? (
              <XCircle size={16} color="var(--cb-red-bright)" />
            ) : c.severity === "warning" ? (
              <AlertTriangle size={16} color="var(--cb-amber)" />
            ) : (
              <Info size={16} color="var(--cb-blue)" />
            )}
          </div>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600 }}>{c.label}</div>
            <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 2 }}>{c.message}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
