import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, AUTH_REQUIRED_EVENT, getToken, PublicConfig, setToken } from "@/api/client";
import type { AuthSessionInfo, Permission } from "@/api/types";
import { BrandMark } from "@/assets/CouchbaseLogo";
import AssistantModelForm from "@/components/settings/AssistantModelForm";
import { fetchSession, logout, SessionContext } from "@/hooks/useAuthSession";

/** Gatekeeper in front of the app:
 *  - first visit with the admin login on and no password yet -> first-run setup
 *    (set the administrator password, then optionally pick the assistant model);
 *  - otherwise, when a bearer is required and none is held -> login page
 *    (administrator password, or an API token for token-only deployments).
 * The session/token lives in sessionStorage for this tab only. Once open, the
 * signed-in principal (username, role, permissions) is provided to the app via
 * SessionContext so navigation and settings can follow the role. */

type Gate = "checking" | "setup" | "login" | "open";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [gate, setGate] = useState<Gate>("checking");
  const [cfg, setCfg] = useState<PublicConfig | null>(null);
  const [session, setSession] = useState<AuthSessionInfo | null>(null);

  const refresh = useCallback(async () => {
    setSession(await fetchSession());
  }, []);

  async function evaluate() {
    try {
      const next = await api.publicConfig();
      setCfg(next);
      if (next.setup_required) setGate("setup");
      else if (next.auth_required && !getToken()) setGate("login");
      else {
        if (next.auth_required) await refresh();
        setGate("open");
      }
    } catch {
      setGate("open");
    }
  }

  const value = useMemo(
    () => ({
      session,
      // No session (auth off / not loaded yet) -> nothing is hidden; the API decides.
      can: (p: Permission) => (session ? session.permissions.includes(p) : true),
      refresh,
      logout,
    }),
    [session, refresh],
  );

  useEffect(() => {
    evaluate();
    const onRequired = () => {
      setToken(null);
      evaluate();
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, onRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onRequired);
  }, []);

  if (gate === "checking") return null;
  if (gate === "open") return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
  if (gate === "setup") return <FirstRunSetup cfg={cfg} onFinished={() => window.location.reload()} />;
  return <LoginPage cfg={cfg} onLoggedIn={() => window.location.reload()} />;
}

function Brand() {
  return (
    <div className="login-brand">
      <BrandMark />
      <div className="login-brand-name">
        Couchbase Labs
        <br />
        Onboarding Agent
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- first run

function FirstRunSetup({ cfg, onFinished }: { cfg: PublicConfig | null; onFinished: () => void }) {
  const [step, setStep] = useState<"password" | "model">("password");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [bootstrap, setBootstrap] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsBootstrap = !!cfg?.bootstrap_token_required;
  const mismatch = confirm.length > 0 && confirm !== password;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError(null);
    try {
      const session = await api.authSetup(password, needsBootstrap ? bootstrap.trim() : undefined);
      setToken(session.token);
      if (cfg?.llm_enabled === false) onFinished();
      else setStep("model");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't set the password.");
    } finally {
      setBusy(false);
    }
  }

  if (step === "model") {
    return (
      <div className="login-shell">
        <div className="login-card login-card-wide">
          <Brand />
          <div className="setup-steps" aria-label="Setup progress">
            <span className="setup-step done">1. Administrator password</span>
            <span className="setup-step active">2. Assistant model</span>
          </div>
          <h1 className="login-title">Choose the assistant's model</h1>
          <p className="login-subtitle">
            The assistant explains validation results and migration strategy. By default it runs on the bundled local
            model; you can instead use a model of your choosing with your own endpoint and credentials. You can change
            this later under Settings.
          </p>
          <AssistantModelForm setupMode onDone={onFinished} />
        </div>
      </div>
    );
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <Brand />
        {cfg?.llm_enabled !== false && (
          <div className="setup-steps" aria-label="Setup progress">
            <span className="setup-step active">1. Administrator password</span>
            <span className="setup-step">2. Assistant model</span>
          </div>
        )}
        <h1 className="login-title">Welcome -- set the administrator password</h1>
        <p className="login-subtitle">
          This is a fresh instance. Choose the password you'll log in with from now on; it's stored only as a hash on
          this server, so write it down somewhere safe.
        </p>
        {needsBootstrap && (
          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="bootstrap-token">API token (from the backend's <code>API_AUTH_TOKEN</code>)</label>
            <input
              id="bootstrap-token"
              type="password"
              autoFocus
              value={bootstrap}
              onChange={(e) => setBootstrap(e.target.value)}
            />
            <span className="field-hint">Proves you operate this instance before it can be claimed.</span>
          </div>
        )}
        <div className="field" style={{ marginBottom: 14 }}>
          <label htmlFor="new-password">New password</label>
          <input
            id="new-password"
            type="password"
            autoFocus={!needsBootstrap}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <span className="field-hint">At least 12 characters.</span>
        </div>
        <div className="field" style={{ marginBottom: 14 }}>
          <label htmlFor="confirm-password">Confirm password</label>
          <input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
          {mismatch && <span className="field-hint" style={{ color: "var(--red)" }}>The passwords don't match.</span>}
        </div>
        {error && (
          <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}
        <button
          className="btn btn-primary login-submit"
          type="submit"
          disabled={busy || password.length < 12 || confirm !== password || (needsBootstrap && !bootstrap.trim())}
        >
          {busy ? "Saving..." : "Set password and continue"}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------- login

function LoginPage({ cfg, onLoggedIn }: { cfg: PublicConfig | null; onLoggedIn: () => void }) {
  const loginEnabled = cfg?.login_enabled !== false;
  const [mode, setMode] = useState<"password" | "token">(loginEnabled ? "password" : "token");
  const [username, setUsername] = useState("admin");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "password") {
        const session = await api.authLogin(username.trim().toLowerCase(), value);
        setToken(session.token);
      } else {
        setToken(value.trim());
        await api.listMigrations();
      }
      onLoggedIn();
    } catch (err) {
      setToken(null);
      if (err instanceof ApiError && err.status === 429) setError(err.message);
      else setError(mode === "password" ? "Wrong username or password." : "That token wasn't accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <Brand />
        <h1 className="login-title">{mode === "password" ? "Log in" : "API token required"}</h1>
        <p className="login-subtitle">
          {mode === "password"
            ? cfg?.ldap_enabled
              ? "Sign in with your local account or your directory (LDAP) username and password. You stay logged in for this browser tab."
              : "Sign in with your username and password. You stay logged in for this browser tab."
            : "This deployment protects its API with a token (the backend's API_AUTH_TOKEN). Paste it to continue; it's kept only for this browser tab."}
        </p>
        {mode === "password" && (
          <div className="field" style={{ marginBottom: 14 }}>
            <label htmlFor="login-username">Username</label>
            <input
              id="login-username"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
        )}
        <div className="field" style={{ marginBottom: 14 }}>
          <label htmlFor="login-secret">{mode === "password" ? "Password" : "API token"}</label>
          <input
            id="login-secret"
            type="password"
            autoFocus
            autoComplete={mode === "password" ? "current-password" : "off"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </div>
        {error && (
          <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}
        <button
          className="btn btn-primary login-submit"
          type="submit"
          disabled={busy || !value.trim() || (mode === "password" && !username.trim())}
        >
          {busy ? "Checking..." : mode === "password" ? "Log in" : "Continue"}
        </button>
        {loginEnabled && (
          <button
            type="button"
            className="btn btn-secondary login-submit"
            style={{ marginTop: 10 }}
            onClick={() => {
              setMode(mode === "password" ? "token" : "password");
              setValue("");
              setError(null);
            }}
          >
            {mode === "password" ? "Use an API token instead" : "Log in with the password instead"}
          </button>
        )}
      </form>
    </div>
  );
}
