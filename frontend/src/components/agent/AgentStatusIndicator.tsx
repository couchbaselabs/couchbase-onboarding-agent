import { useAgentStatus } from "@/hooks/useAgentStatus";
import type { AgentHealthState } from "@/api/types";

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

export default function AgentStatusIndicator() {
  const { status, detail } = useAgentStatus();
  return (
    <div className="status-pill" style={{ marginTop: 10 }} title={detail} role="status">
      <span className={DOT_CLASS[status]} />
      {STATE_LABEL[status]}
    </div>
  );
}
