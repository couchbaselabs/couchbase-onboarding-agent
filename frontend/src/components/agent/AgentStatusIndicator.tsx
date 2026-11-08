import { useAgentStatus } from "@/hooks/useAgentStatus";
import type { AgentHealthState, LlmCompute } from "@/api/types";

const DOT_CLASS: Record<AgentHealthState, string> = {
  ready: "status-dot",
  waiting: "status-dot waiting",
  error: "status-dot down",
  disabled: "status-dot",
};

const STATE_LABEL: Record<AgentHealthState, string> = {
  ready: "Onboarding agent ready",
  waiting: "Onboarding agent starting...",
  error: "Onboarding agent unavailable",
  disabled: "Assistant off (migrations run)",
};

const COMPUTE_TAG: Record<LlmCompute, string | null> = {
  gpu: "GPU",
  cpu: "CPU",
  partial: "GPU+CPU",
  unknown: null, // nothing loaded yet; don't guess
};

export default function AgentStatusIndicator() {
  const { status, detail, provider_label, model, local, compute, compute_detail } = useAgentStatus();
  const computeTag = status === "ready" && local !== false && compute ? COMPUTE_TAG[compute] : null;
  const titleParts = [
    model ? `${provider_label ?? ""} ${model}${local === false ? " (hosted)" : ""} -- ${detail}` : detail,
    compute_detail,
  ].filter(Boolean);
  const label = status === "ready" && local === false
    ? `Assistant ready (${provider_label ?? "hosted"})`
    : STATE_LABEL[status];
  return (
    <div className="status-pill" style={{ marginTop: 10 }} title={titleParts.join("\n")} role="status">
      <span className={DOT_CLASS[status]} />
      {label}
      {computeTag && <span className="status-compute-tag">{computeTag}</span>}
    </div>
  );
}
