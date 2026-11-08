import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { LdapConfig, LdapPreset, LdapTestResult } from "@/api/types";

const EMPTY: LdapConfig = {
  enabled: false,
  server_url: "",
  start_tls: false,
  verify_tls: true,
  bind_dn: "",
  bind_password: "",
  user_base_dn: "",
  user_filter: "(&(objectClass=user)(sAMAccountName={username}))",
  username_attribute: "sAMAccountName",
  group_attribute: "memberOf",
  group_base_dn: "",
  group_filter: "",
  role_mappings: [],
  default_role: "",
  timeout_s: 10,
};

/** Settings -> LDAP: directory sign-in with group -> role mapping. Local
 * accounts keep working alongside; a username that isn't a local user is
 * tried against the directory. */
export default function LdapSettings() {
  const [cfg, setCfg] = useState<LdapConfig>(EMPTY);
  const [presets, setPresets] = useState<Record<string, LdapPreset>>({});
  const [roles, setRoles] = useState<string[]>([]);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [test, setTest] = useState<LdapTestResult | null>(null);
  const [testUser, setTestUser] = useState("");
  const [testPw, setTestPw] = useState("");

  useEffect(() => {
    api
      .ldapSettings()
      .then((r) => {
        setCfg({ ...EMPTY, ...r.config, bind_password: "" });
        setPresets(r.presets);
        setRoles(r.roles);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Couldn't load the LDAP settings."));
  }, []);

  const set = <K extends keyof LdapConfig>(k: K, v: LdapConfig[K]) => setCfg((c) => ({ ...c, [k]: v }));
  const ldaps = cfg.server_url.startsWith("ldaps://");

  async function save() {
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const r = await api.saveLdap(cfg);
      setCfg({ ...EMPTY, ...r.config, bind_password: "" });
      setNotice(r.config.enabled ? "Saved. Directory users can sign in now." : "Saved. Directory sign-in is off.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save.");
    } finally {
      setBusy(null);
    }
  }

  async function runTest() {
    setBusy("test");
    setError(null);
    setTest(null);
    try {
      setTest(await api.testLdap(cfg, testUser || undefined, testPw || undefined));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The test couldn't run.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <section className="card">
        <div className="card-title-row">
          <div className="card-title">Directory sign-in</div>
          <label className="checkbox-row" style={{ margin: 0 }}>
            <input type="checkbox" checked={cfg.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            <span>Enabled</span>
          </label>
        </div>
        <p className="field-hint" style={{ marginBottom: 14 }}>
          Users who aren't local accounts are looked up in the directory and sign in with their directory password.
          They get the role mapped from their groups below; nothing about them is stored here beyond the live session.
          Local accounts (including <code>admin</code>) keep working, so you can't lock yourself out.
        </p>
        {error && (
          <div className="helper-banner helper-banner-danger" role="alert" style={{ marginBottom: 10 }}>
            {error}
          </div>
        )}
        {notice && (
          <div className="helper-banner helper-banner-success" role="status" style={{ marginBottom: 10 }}>
            {notice}
          </div>
        )}
        <div className="form-grid">
          <div className="field">
            <label htmlFor="ldap-preset">Directory type</label>
            <select
              id="ldap-preset"
              defaultValue=""
              onChange={(e) => {
                const p = presets[e.target.value];
                if (!p) return;
                setCfg((c) => ({
                  ...c,
                  user_filter: p.user_filter,
                  group_attribute: p.group_attribute,
                  group_filter: p.group_filter,
                  username_attribute: p.username_attribute,
                }));
              }}
            >
              <option value="">Apply a preset…</option>
              {Object.entries(presets).map(([k, p]) => (
                <option key={k} value={k}>
                  {p.label}
                </option>
              ))}
            </select>
            <span className="field-hint">Fills the filters below with sensible defaults; adjust as needed.</span>
          </div>
          <div className="field">
            <label htmlFor="ldap-url">Server URL</label>
            <input id="ldap-url" placeholder="ldaps://dc01.corp.example:636" value={cfg.server_url} onChange={(e) => set("server_url", e.target.value)} />
            <span className="field-hint">
              Use <code>ldaps://</code> (636) or <code>ldap://</code> with StartTLS. The server certificate is verified
              against the public CAs plus any you added under Certificates.
            </span>
          </div>
          <div className="checkbox-row">
            <input id="ldap-starttls" type="checkbox" checked={cfg.start_tls} disabled={ldaps} onChange={(e) => set("start_tls", e.target.checked)} />
            <label htmlFor="ldap-starttls">Use StartTLS on an ldap:// connection</label>
          </div>
          <div className="checkbox-row">
            <input id="ldap-verify" type="checkbox" checked={cfg.verify_tls} onChange={(e) => set("verify_tls", e.target.checked)} />
            <label htmlFor="ldap-verify">
              Verify the server certificate{" "}
              {!cfg.verify_tls && <span className="badge badge-warning" style={{ marginLeft: 6 }}>insecure</span>}
            </label>
          </div>
          <div className="field">
            <label htmlFor="ldap-bind">Bind DN (service account)</label>
            <input id="ldap-bind" placeholder="cn=svc-onboarding,ou=service,dc=corp,dc=example" value={cfg.bind_dn} onChange={(e) => set("bind_dn", e.target.value)} />
            <span className="field-hint">Read-only account used to find users and groups. Leave empty for an anonymous bind.</span>
          </div>
          <div className="field">
            <label htmlFor="ldap-bindpw">Bind password</label>
            <input
              id="ldap-bindpw"
              type="password"
              autoComplete="new-password"
              placeholder={cfg.bind_password_set ? "•••••••• (stored; leave blank to keep)" : ""}
              value={cfg.bind_password}
              onChange={(e) => set("bind_password", e.target.value)}
            />
            <span className="field-hint">Stored encrypted with the state encryption key.</span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-title">Users and groups</div>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="ldap-ubase">User base DN</label>
            <input id="ldap-ubase" placeholder="ou=people,dc=corp,dc=example" value={cfg.user_base_dn} onChange={(e) => set("user_base_dn", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="ldap-ufilter">User filter</label>
            <input id="ldap-ufilter" className="mono" value={cfg.user_filter} onChange={(e) => set("user_filter", e.target.value)} />
            <span className="field-hint">
              <code>{"{username}"}</code> is replaced with what the person types (escaped).
            </span>
          </div>
          <div className="field">
            <label htmlFor="ldap-gattr">Group attribute on the user</label>
            <input id="ldap-gattr" className="mono" placeholder="memberOf" value={cfg.group_attribute} onChange={(e) => set("group_attribute", e.target.value)} />
            <span className="field-hint">Active Directory and OpenLDAP with the memberof overlay list the user's groups here.</span>
          </div>
          <div className="field">
            <label htmlFor="ldap-gfilter">Group search filter (fallback)</label>
            <input id="ldap-gfilter" className="mono" placeholder="(&(objectClass=groupOfNames)(member={user_dn}))" value={cfg.group_filter} onChange={(e) => set("group_filter", e.target.value)} />
            <span className="field-hint">
              Used when the attribute above is empty. <code>{"{user_dn}"}</code> / <code>{"{username}"}</code> are substituted.
            </span>
          </div>
          <div className="field">
            <label htmlFor="ldap-gbase">Group base DN</label>
            <input id="ldap-gbase" placeholder="ou=groups,dc=corp,dc=example (defaults to the user base)" value={cfg.group_base_dn} onChange={(e) => set("group_base_dn", e.target.value)} />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-title">Group → role mapping</div>
        <p className="field-hint" style={{ marginBottom: 12 }}>
          The first mapping whose group the user belongs to decides their role (so put the most privileged first).
          Users in none of these groups get the default role, or are denied if there isn't one.
        </p>
        <div className="mapping-list">
          {cfg.role_mappings.map((m, i) => (
            <div key={i} className="mapping-row">
              <input
                className="mono"
                placeholder="cn=cb-operators,ou=groups,dc=corp,dc=example"
                value={m.group}
                aria-label={`Group DN ${i + 1}`}
                onChange={(e) => {
                  const next = [...cfg.role_mappings];
                  next[i] = { ...m, group: e.target.value };
                  set("role_mappings", next);
                }}
              />
              <span className="field-hint">→</span>
              <select
                value={m.role}
                aria-label={`Role ${i + 1}`}
                onChange={(e) => {
                  const next = [...cfg.role_mappings];
                  next[i] = { ...m, role: e.target.value };
                  set("role_mappings", next);
                }}
              >
                {roles.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="icon-btn"
                aria-label="Remove mapping"
                title="Remove"
                onClick={() => set("role_mappings", cfg.role_mappings.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <div className="btn-wrap" style={{ marginTop: 8 }}>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => set("role_mappings", [...cfg.role_mappings, { group: "", role: roles.includes("read_only") ? "read_only" : roles[0] ?? "" }])}
          >
            Add mapping
          </button>
        </div>
        <div className="field" style={{ marginTop: 14 }}>
          <label htmlFor="ldap-default">Default role for directory users in no mapped group</label>
          <select id="ldap-default" value={cfg.default_role} onChange={(e) => set("default_role", e.target.value)}>
            <option value="">(none -- deny)</option>
            {roles.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
      </section>

      <section className="card">
        <div className="card-title">Test</div>
        <p className="field-hint" style={{ marginBottom: 12 }}>
          Checks the service-account bind with the values above (unsaved changes included). Add a directory username
          to see the groups and role it would get; add its password to try a full login.
        </p>
        <div className="form-grid-2">
          <div className="field">
            <label htmlFor="ldap-tu">Directory username (optional)</label>
            <input id="ldap-tu" autoCapitalize="none" value={testUser} onChange={(e) => setTestUser(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="ldap-tp">Their password (optional)</label>
            <input id="ldap-tp" type="password" autoComplete="off" value={testPw} onChange={(e) => setTestPw(e.target.value)} />
          </div>
        </div>
        {test && (
          <div className={`helper-banner ${test.ok ? "helper-banner-success" : "helper-banner-danger"}`} role="status" style={{ marginTop: 6 }}>
            <div>
              {test.ok ? "Connected" : "Problem"}
              {test.connection && ` -- bound as ${test.connection.bound_as}`}
              {test.login && `; login ${test.login}`}
              {test.message && ` -- ${test.message}`}
            </div>
            {test.user && (
              <div className="field-hint mono" style={{ marginTop: 6 }}>
                {test.user.dn}
                <br />
                groups: {test.user.groups.join(", ") || "-"}
                <br />
                role: {test.user.role ?? "(none)"}
              </div>
            )}
          </div>
        )}
        <div className="btn-wrap" style={{ marginTop: 12 }}>
          <button type="button" className="btn btn-secondary" disabled={busy !== null || !cfg.server_url} onClick={runTest}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
        </div>
      </section>
    </form>
  );
}
