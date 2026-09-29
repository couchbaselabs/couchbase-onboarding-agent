import { Check } from "lucide-react";

const STEPS = ["Source", "Destination & Mode", "Validate", "Review & Approve"];

export default function StepIndicator({ step }: { step: number }) {
  return (
    <ol className="steps" aria-label="Wizard steps" style={{ listStyle: "none", padding: 0 }}>
      {STEPS.map((label, i) => {
        const state = i < step ? "done" : i === step ? "active" : "pending";
        return (
          <li key={label} style={{ display: "contents" }}>
            <div className="step" data-state={state} aria-current={state === "active" ? "step" : undefined}>
              <span className="step-num">{state === "done" ? <Check size={14} strokeWidth={3} /> : i + 1}</span>
              {label}
            </div>
            {i < STEPS.length - 1 && <div className="step-line" aria-hidden="true" />}
          </li>
        );
      })}
    </ol>
  );
}
