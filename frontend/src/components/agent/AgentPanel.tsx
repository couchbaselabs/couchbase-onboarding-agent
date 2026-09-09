import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, MessageSquareText, Send, Wrench, X } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import type { BottleneckFinding } from "@/api/types";

interface ChatTurn {
  role: "user" | "assistant" | "alert";
  content: string;
  autoRemediated?: boolean;
}

function findingTurn(f: BottleneckFinding): ChatTurn {
  const headline = f.auto_remediated ? "Auto-remediated" : "Bottleneck detected";
  return {
    role: "alert",
    autoRemediated: f.auto_remediated,
    content: `${headline}: ${f.message}\n${f.suggestion}`,
  };
}

export default function AgentPanel() {
  const { id: migrationId } = useParams();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sending, setSending] = useState(false);

  // Live-tail the currently viewed migration (if any) purely to catch new
  // bottleneck findings as they land, independent of anything the user is
  // typing -- these are pushed proactively rather than in response to a question.
  const { record } = useMigrationSocket(migrationId || "*");
  const seenFindingIds = useRef<Set<string>>(new Set());
  const seededForId = useRef<string | null>(null);

  useEffect(() => {
    if (!migrationId || !record || record.migration_id !== migrationId) return;

    if (seededForId.current !== migrationId) {
      // First snapshot seen for this migration in this session: treat any
      // findings already on it as history, not a fresh alert to announce.
      seenFindingIds.current = new Set(record.bottleneck_findings.map((f) => f.finding_id));
      seededForId.current = migrationId;
      return;
    }

    const fresh = record.bottleneck_findings.filter((f) => !seenFindingIds.current.has(f.finding_id));
    if (fresh.length === 0) return;
    fresh.forEach((f) => seenFindingIds.current.add(f.finding_id));

    setOpen(true);
    setTurns((prev) => [...prev, ...fresh.map(findingTurn)]);
  }, [record, migrationId]);

  async function send() {
    const text = message.trim();
    if (!text || sending) return;
    setTurns((prev) => [...prev, { role: "user", content: text }]);
    setMessage("");
    setSending(true);
    try {
      const resp = await api.chat(text, migrationId);
      setTurns((prev) => [...prev, { role: "assistant", content: resp.reply }]);
    } catch (err) {
      const detail = err instanceof ApiError ? err.message : "Something went wrong reaching the agent.";
      setTurns((prev) => [...prev, { role: "assistant", content: detail }]);
    } finally {
      setSending(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="cb-btn cb-btn-primary"
        style={{
          position: "fixed", bottom: 24, right: 24, borderRadius: 999,
          height: 52, padding: "0 22px", display: "flex", alignItems: "center", justifyContent: "center",
          gap: 9, fontSize: 14, boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
        }}
        aria-label="Ask the agent"
      >
        <MessageSquareText size={20} />
        Ask the agent
      </button>
    );
  }

  return (
    <div
      style={{
        position: "fixed", bottom: 24, right: 24, width: 360, height: 480,
        background: "var(--bg-1)", border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)", display: "flex", flexDirection: "column", overflow: "hidden",
        boxShadow: "0 12px 40px rgba(0,0,0,0.5)",
      }}
    >
      <div
        style={{
          padding: "12px 14px", borderBottom: "1px solid var(--border-subtle)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}
      >
        <div style={{ fontSize: 13, fontWeight: 700 }}>Ask the agent</div>
        <button onClick={() => setOpen(false)} style={{ background: "none", border: "none", color: "var(--text-muted)" }}>
          <X size={16} />
        </button>
      </div>
      <div className="cb-scrollbar" style={{ flex: 1, overflowY: "auto", padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
        {turns.length === 0 && (
          <div style={{ fontSize: 12.5, color: "var(--text-primary)", display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              Welcome to the Couchbase Onboarding Agent. I help plan and de-risk your migration from MongoDB,
              DynamoDB, Redis, Cassandra, or Cosmos DB into Couchbase.
            </div>
            <div>
              <div style={{ fontWeight: 700, marginBottom: 4 }}>Automatically, I:</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                <div>- Watch throughput and surface bottleneck alerts right here as they happen</div>
                <div>- Auto-throttle concurrency for source throttling or destination backpressure</div>
                <div>- Recall similar past incidents from Couchbase-backed memory</div>
              </div>
            </div>
            <div>
              <div style={{ fontWeight: 700, marginBottom: 4 }}>Ask me to:</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                <div>- Recommend a strategy (one-time load, continuous CDC, or hybrid) for your use case</div>
                <div>- Explain a validation failure or warning and how to fix it</div>
                <div>- Reason through how your source data will map into Couchbase documents</div>
              </div>
            </div>
          </div>
        )}
        {turns.map((t, i) =>
          t.role === "alert" ? (
            <div
              key={i}
              style={{
                alignSelf: "flex-start", maxWidth: "92%", display: "flex", gap: 8,
                background: "var(--bg-2)", border: `1px solid ${t.autoRemediated ? "var(--cb-teal)" : "var(--cb-amber)"}`,
                borderRadius: 10, padding: "8px 11px", fontSize: 12, whiteSpace: "pre-wrap",
              }}
            >
              {t.autoRemediated ? (
                <Wrench size={14} color="var(--cb-teal)" style={{ flexShrink: 0, marginTop: 1 }} />
              ) : (
                <AlertTriangle size={14} color="var(--cb-amber)" style={{ flexShrink: 0, marginTop: 1 }} />
              )}
              <div>{t.content}</div>
            </div>
          ) : (
            <div
              key={i}
              style={{
                alignSelf: t.role === "user" ? "flex-end" : "flex-start",
                maxWidth: "85%",
                background: t.role === "user" ? "var(--cb-red)" : "var(--bg-2)",
                color: t.role === "user" ? "white" : "var(--text-primary)",
                borderRadius: 10, padding: "8px 11px", fontSize: 12.5, whiteSpace: "pre-wrap",
              }}
            >
              {t.content}
            </div>
          ),
        )}
        {sending && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Thinking...</div>}
      </div>
      <div style={{ padding: 10, borderTop: "1px solid var(--border-subtle)", display: "flex", gap: 8 }}>
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask a question..."
          style={{ flex: 1 }}
        />
        <button onClick={send} className="cb-btn cb-btn-primary" style={{ padding: "8px 10px" }} disabled={sending}>
          <Send size={14} />
        </button>
      </div>
    </div>
  );
}
