import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, MessageSquareText, Send, Wrench, X } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useMigrationSocket } from "@/hooks/useMigrationSocket";
import type { BottleneckFinding } from "@/api/types";
import { BrandMark } from "@/assets/CouchbaseLogo";

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
      <button onClick={() => setOpen(true)} className="btn btn-primary agent-fab" aria-label="Ask the agent">
        <MessageSquareText size={18} />
        Ask the agent
      </button>
    );
  }

  return (
    <div className="agent-panel" role="dialog" aria-label="Ask the agent">
      <div className="agent-panel-header">
        <BrandMark />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13.5, fontWeight: 700 }}>Ask the agent</div>
          <div className="muted" style={{ fontSize: 11.5 }}>
            {migrationId ? "About this migration" : "Planning, cutover, rollback"}
          </div>
        </div>
        <button className="icon-btn" onClick={() => setOpen(false)} aria-label="Close">
          <X size={15} />
        </button>
      </div>
      <div className="cb-scrollbar" style={{ flex: 1, overflowY: "auto", padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
        {turns.length === 0 && (
          <div style={{ fontSize: 12.5, lineHeight: 1.55, display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              Welcome to the Couchbase Onboarding Agent. I help plan and de-risk your migration from MongoDB,
              DynamoDB, Redis, Cassandra, Cosmos DB, or another Couchbase cluster into Couchbase.
            </div>
            <div>
              <div className="stat-label" style={{ marginBottom: 4 }}>Automatically, I</div>
              <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-muted)" }}>
                <li>Watch throughput and surface bottleneck alerts right here as they happen</li>
                <li>Auto-throttle concurrency for source throttling or destination backpressure</li>
                <li>Recall similar past incidents from Couchbase-backed memory</li>
              </ul>
            </div>
            <div>
              <div className="stat-label" style={{ marginBottom: 4 }}>Ask me to</div>
              <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-muted)" }}>
                <li>Recommend a strategy (one-time load, continuous CDC, or hybrid) for your use case</li>
                <li>Explain a validation failure or warning and how to fix it</li>
                <li>Walk through cutover, rollback, or falling back to the source</li>
                <li>Reason through how your source data will map into Couchbase documents</li>
              </ul>
            </div>
          </div>
        )}
        {turns.map((t, i) =>
          t.role === "alert" ? (
            <div
              key={i}
              className={`helper-banner${t.autoRemediated ? " helper-banner-success" : ""}`}
              style={{ alignSelf: "flex-start", maxWidth: "92%", whiteSpace: "pre-wrap" }}
            >
              {t.autoRemediated ? <Wrench size={14} /> : <AlertTriangle size={14} />}
              <div>{t.content}</div>
            </div>
          ) : (
            <div key={i} className={`agent-bubble ${t.role}`}>
              {t.content}
            </div>
          ),
        )}
        {sending && <div className="muted">Thinking...</div>}
      </div>
      <div style={{ padding: 10, borderTop: "1px solid var(--border)", display: "flex", gap: 8 }}>
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask a question..."
          aria-label="Message"
          style={{ flex: 1 }}
        />
        <button onClick={send} className="btn btn-primary" style={{ padding: "8px 11px" }} disabled={sending} aria-label="Send">
          <Send size={14} />
        </button>
      </div>
    </div>
  );
}
