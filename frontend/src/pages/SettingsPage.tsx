import { useEffect, useState } from "react";
import { api, ApiError, setToken } from "@/api/client";
import type { AuthSessionInfo } from "@/api/types";
import AssistantModelForm from "@/components/settings/AssistantModelForm";

export default function SettingsPage() {
  const [session, setSession] = useState<AuthSessionInfo | null>(null);

  useEffect(() => {
    api.authSession().then(setSession).catch(() => setSession(null));
  }, []);

  return (
    <div className="page-narrow">
      <div className="page-header">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="page-subtitle">The assistant's model and this instance's administrator login.</p>
        </div>
      </div>

      <div className="stack">
        <section className="card">
          <div className="card-title">Assistant model</div>
          <p className="field-hint" style={{ marginBottom: 14 }}>
            Which model answers in the "Ask the agent" panel. The bundled local model keeps prompts on this host; a
            hosted provider uses the endpoint and credentials you supply here.
          </p>
          <AssistantModelForm />
        </section>

        {session?.login_enabled && (
          <section className="card">
            <div className="card-title">Administrator password</div>
            <ChangePassword session={session} />
          </section>
        )}
      </div>
    </div>
  );
}

function ChangePassword({ session }: { session: AuthSessionInfo }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const mismatch = confirm.length > 0 && confirm !== next;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await api.changePassword(current, next);
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form-grid" onSubmit={submit}>
      {session.password_set_at && (
        <span className="field-hint">Last set {new Date(session.password_set_at).toLocaleString()}. Changing it signs out every other session.</span>
      )}
      {session.method === "token" && (
        <div className="helper-banner helper-banner-neutral">
          You're signed in with the API token; the current password is still required to change it.
        </div>
      )}
      <div className="field">
        <label htmlFor="pw-current">Current password</label>
        <input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="pw-next">New password</label>
        <input id="pw-next" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        <span className="field-hint">At least 12 characters.</span>
      </div>
      <div className="field">
        <label htmlFor="pw-confirm">Confirm new password</label>
        <input id="pw-confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        {mismatch && <span className="field-hint" style={{ color: "var(--red)" }}>The passwords don't match.</span>}
      </div>
      {error && (
        <div className="helper-banner helper-banner-danger" role="alert">
          {error}
        </div>
      )}
      {done && (
        <div className="helper-banner helper-banner-success" role="status">
          Password changed.
        </div>
      )}
      <div className="btn-wrap">
        <button className="btn btn-primary" type="submit" disabled={busy || !current || next.length < 12 || confirm !== next}>
          {busy ? "Saving..." : "Change password"}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={async () => {
            try {
              await api.authLogout();
            } finally {
              setToken(null);
              window.location.reload();
            }
          }}
        >
          Sign out
        </button>
      </div>
    </form>
  );
}
