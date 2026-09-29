import { useEffect, useState } from "react";
import { KeyRound } from "lucide-react";
import { api, AUTH_REQUIRED_EVENT, getToken, setToken } from "@/api/client";

/** Asks for the API token when the backend requires one (API_AUTH_TOKEN). The
 * token lives in sessionStorage for this tab only. */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [needsToken, setNeedsToken] = useState(false);
  const [checked, setChecked] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .publicConfig()
      .then((cfg) => setNeedsToken(cfg.auth_required && !getToken()))
      .catch(() => setNeedsToken(false))
      .finally(() => setChecked(true));
    const onRequired = () => {
      setToken(null);
      setNeedsToken(true);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, onRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onRequired);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setToken(value.trim());
    setError(null);
    try {
      await api.listMigrations();
      setNeedsToken(false);
      window.location.reload();
    } catch {
      setToken(null);
      setError("That token wasn't accepted.");
    }
  }

  if (!checked) return null;
  if (!needsToken) return <>{children}</>;
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <form className="cb-card" onSubmit={submit} style={{ width: "min(420px, 100%)", display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, fontSize: 15 }}>
          <KeyRound size={18} /> API token required
        </div>
        <div style={{ fontSize: 12.5, color: "var(--text-secondary)" }}>
          This deployment protects its API with a token (the backend's <code>API_AUTH_TOKEN</code>). Paste it to
          continue; it's kept only for this browser tab.
        </div>
        <input type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} aria-label="API token" />
        {error && <div style={{ color: "var(--cb-red-bright)", fontSize: 12.5 }}>{error}</div>}
        <button className="cb-btn cb-btn-primary" type="submit" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </div>
  );
}
