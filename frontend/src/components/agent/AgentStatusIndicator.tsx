import { colors } from "@/theme/tokens";
import { useAgentStatus } from "@/hooks/useAgentStatus";
import type { AgentHealthState } from "@/api/types";

const STATE_COLOR: Record<AgentHealthState, string> = {
  ready: colors.cbGreen,
  waiting: colors.cbAmber,
  error: colors.cbRedBright,
};

const STATE_LABEL: Record<AgentHealthState, string> = {
  ready: "Ready",
  waiting: "Waiting",
  error: "Error",
};

export default function AgentStatusIndicator() {
  const { status, detail } = useAgentStatus();
  const color = STATE_COLOR[status];

  return (
    <div style={{ fontSize: 11, color: "var(--text-muted)" }} title={detail}>
      <div style={{ fontWeight: 700, marginBottom: 6 }}>Couchbase Onboarding Agent Status:</div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: color,
            boxShadow: `0 0 5px ${color}`,
            flexShrink: 0,
          }}
        />
        <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>{STATE_LABEL[status]}</span>
      </div>
    </div>
  );
}
