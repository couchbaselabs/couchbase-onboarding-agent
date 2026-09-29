import { useEffect, useState } from "react";
import { api, AUTH_REQUIRED_EVENT, getToken, setToken } from "@/api/client";
import { BrandMark } from "@/assets/CouchbaseLogo";

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
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <BrandMark />
          <div className="login-brand-name">
            Couchbase
            <br />
            Onboarding Agent
          </div>
        </div>
        <h1 className="login-title">API token required</h1>
        <p className="login-subtitle">
          This deployment protects its API with a token (the backend's <code>API_AUTH_TOKEN</code>). Paste it to
          continue; it's kept only for this browser tab.
        </p>
        <div className="field" style={{ marginBottom: 14 }}>
          <label htmlFor="api-token">API token</label>
          <input id="api-token" type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} />
        </div>
        {error && (
          <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}
        <button className="btn btn-primary login-submit" type="submit" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </div>
  );
}
