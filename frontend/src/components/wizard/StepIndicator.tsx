import { Check } from "lucide-react";

const STEPS = ["Source", "Destination & Mode", "Validate", "Review & Approve"];

export default function StepIndicator({ step }: { step: number }) {
  return (
    <div style={{ display: "flex", gap: 0, marginBottom: 28 }}>
      {STEPS.map((label, i) => {
        const state = i < step ? "done" : i === step ? "active" : "pending";
        return (
          <div key={label} style={{ display: "flex", alignItems: "center", flex: i < STEPS.length - 1 ? 1 : "0 0 auto" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div
                style={{
                  width: 26, height: 26, borderRadius: "50%",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 12, fontWeight: 700,
                  background: state === "pending" ? "var(--bg-2)" : "var(--cb-red)",
                  color: state === "pending" ? "var(--text-muted)" : "white",
                  border: state === "active" ? "2px solid var(--cb-red-bright)" : "none",
                }}
              >
                {state === "done" ? <Check size={14} /> : i + 1}
              </div>
              <span style={{ fontSize: 12.5, fontWeight: 600, color: state === "pending" ? "var(--text-muted)" : "var(--text-primary)" }}>
                {label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <div style={{ flex: 1, height: 1, margin: "0 14px", background: "var(--border-subtle)" }} />
            )}
          </div>
        );
      })}
    </div>
  );
}
