import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "@/api/client";
import type { AuthSessionInfo, Permission } from "@/api/types";
import AssistantModelForm from "@/components/settings/AssistantModelForm";
import RbacSettings from "@/components/settings/RbacSettings";
import LdapSettings from "@/components/settings/LdapSettings";
import CertificateSettings from "@/components/settings/CertificateSettings";
import LogForwardingSettings from "@/components/settings/LogForwardingSettings";
import { logout, useAuthSession } from "@/hooks/useAuthSession";

type TabId = "model" | "account" | "rbac" | "ldap" | "certificates" | "logs";

interface Tab {
  id: TabId;
  label: string;
  blurb: string;
  /** permission needed to see the tab; undefined = everyone signed in */
  needs?: Permission;
  /** only for password sessions (not the API token / auth off) */
  sessionOnly?: boolean;
}

const TABS: Tab[] = [
  { id: "model", label: "Assistant model", blurb: "Which model answers in the \"Ask the agent\" panel.", needs: "settings:write" },
  { id: "account", label: "Account", blurb: "Your password and session.", sessionOnly: true },
  { id: "rbac", label: "RBAC & Roles", blurb: "Who can sign in and what each role may do.", needs: "users:manage" },
  { id: "ldap", label: "LDAP", blurb: "Sign in with a directory (Active Directory, OpenLDAP) and map groups to roles.", needs: "settings:write" },
  { id: "certificates", label: "Certificates", blurb: "CAs this app trusts, and the certificate its HTTPS UI serves.", needs: "settings:write" },
  { id: "logs", label: "Log forwarding", blurb: "Ship the application and audit logs to your log platform.", needs: "settings:write" },
];

export default function SettingsPage() {
  const { session, can } = useAuthSession();
  const [params, setParams] = useSearchParams();

  const tabs = useMemo(
    () =>
      TABS.filter((t) => (t.needs ? can(t.needs) : true)).filter((t) =>
        t.sessionOnly ? !session || session.method === "session" : true,
      ),
    [session, can],
  );
  const requested = params.get("tab") as TabId | null;
  const active = tabs.find((t) => t.id === requested) ?? tabs[0];

  return (
    <div className="page-narrow settings-page">
      <div className="page-header">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="page-subtitle">{active?.blurb ?? "Nothing here needs your attention."}</p>
        </div>
      </div>

      <div className="settings-layout">
        <nav className="settings-tabs" aria-label="Settings sections">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`settings-tab${active?.id === t.id ? " active" : ""}`}
              onClick={() => setParams({ tab: t.id })}
            >
              {t.label}
            </button>
          ))}
        </nav>

        <div className="settings-body">
          {active?.id === "model" && (
            <section className="card">
              <div className="card-title">Assistant model</div>
              <p className="field-hint" style={{ marginBottom: 14 }}>
                Which model answers in the "Ask the agent" panel. The bundled local model keeps prompts on this host; a
                hosted provider uses the endpoint and credentials you supply here.
              </p>
              <AssistantModelForm />
            </section>
          )}
          {active?.id === "account" && session && (
            <section className="card">
              <div className="card-title">
                Your password{" "}
                <span className="field-hint" style={{ fontWeight: 400 }}>
                  -- signed in as <strong>{session.username}</strong> ({session.role.replace("_", " ")})
                </span>
              </div>
              {session.source === "ldap" ? (
                <p className="field-hint">You signed in with a directory (LDAP) account; change your password in your directory.</p>
              ) : session.login_enabled ? (
                <ChangePassword session={session} />
              ) : (
                <p className="field-hint">The password login is disabled for this deployment (ADMIN_LOGIN_ENABLED=false).</p>
              )}
            </section>
          )}
          {active?.id === "rbac" && <RbacSettings />}
          {active?.id === "ldap" && <LdapSettings />}
          {active?.id === "certificates" && <CertificateSettings />}
          {active?.id === "logs" && <LogForwardingSettings />}
          {!active && (
            <section className="card">
              <p className="field-hint">Your role has no settings to manage here.</p>
            </section>
          )}
        </div>
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
        <button type="button" className="btn btn-secondary" onClick={logout}>
          Log out
        </button>
      </div>
    </form>
  );
}
