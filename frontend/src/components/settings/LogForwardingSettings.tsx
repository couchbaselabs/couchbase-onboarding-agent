import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { LogDestination, LogForwardConfig, LogForwardSettingsResponse, LogForwardStatus } from "@/api/types";

const EMPTY: LogForwardConfig = {
  enabled: false,
  destination: "splunk_hec",
  settings: {},
  min_level: "INFO",
  include_audit: true,
  include_app: true,
  batch_size: 50,
  flush_interval_s: 5,
  hostname: "",
  extra_fields: {},
};

/** Settings -> Log forwarding: ship the application log and the auth audit
 * trail to one destination -- Splunk HEC, Datadog, Elasticsearch/OpenSearch,
 * Grafana Loki or RFC 5424 syslog. */
export default function LogForwardingSettings() {
  const [meta, setMeta] = useState<LogForwardSettingsResponse | null>(null);
  const [cfg, setCfg] = useState<LogForwardConfig>(EMPTY);
  const [status, setStatus] = useState<LogForwardStatus | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [extraText, setExtraText] = useState("");

  useEffect(() => {
    api
      .logForwarding()
      .then((r) => {
        setMeta(r);
        setCfg({ ...EMPTY, ...r.config });
        setStatus(r.status);
        setExtraText(
          Object.entries(r.config.extra_fields ?? {})
            .map(([k, v]) => `${k}=${v}`)
            .join(", "),
        );
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Couldn't load the log forwarding settings."));
  }, []);

  // Refresh delivery counters while the tab is open and forwarding is on.
  useEffect(() => {
    if (!status?.active) return;
    const t = setInterval(() => api.logForwarding().then((r) => setStatus(r.status)).catch(() => {}), 5000);
    return () => clearInterval(t);
  }, [status?.active]);

  const dest = meta?.destinations[cfg.destination];
  const set = <K extends keyof LogForwardConfig>(k: K, v: LogForwardConfig[K]) => setCfg((c) => ({ ...c, [k]: v }));
  const setField = (k: string, v: string) => setCfg((c) => ({ ...c, settings: { ...c.settings, [k]: v } }));

  function withExtras(c: LogForwardConfig): LogForwardConfig {
    const extra: Record<string, string> = {};
    for (const part of extraText.split(",")) {
      const [k, ...rest] = part.split("=");
      if (k?.trim() && rest.length) extra[k.trim()] = rest.join("=").trim();
    }
    return { ...c, extra_fields: extra };
  }

  function chooseDestination(d: LogDestination) {
    const fields = meta?.destinations[d]?.fields ?? [];
    const defaults: Record<string, string> = {};
    for (const f of fields) if (f.default) defaults[f.key] = f.default;
    setCfg((c) => ({ ...c, destination: d, settings: d === meta?.config.destination ? { ...defaults, ...meta.config.settings } : defaults }));
  }

  async function save() {
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const r = await api.saveLogForwarding(withExtras(cfg));
      setCfg({ ...EMPTY, ...r.config });
      setStatus(r.status);
      setNotice(r.config.enabled ? `Saved. Forwarding to ${dest?.label}.` : "Saved. Forwarding is off.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save.");
    } finally {
      setBusy(null);
    }
  }

  async function test() {
    setBusy("test");
    setError(null);
    setNotice(null);
    try {
      const r = await api.testLogForwarding(withExtras(cfg));
      setNotice(`Test event delivered to ${r.destination} in ${r.ms} ms.`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The test event couldn't be sent.");
    } finally {
      setBusy(null);
    }
  }

  if (!meta) return <section className="card">{error ?? "Loading…"}</section>;

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
          <div className="card-title">Destination</div>
          <label className="checkbox-row" style={{ margin: 0 }}>
            <input type="checkbox" checked={cfg.enabled} onChange={(e) => set("enabled", e.target.checked)} />
            <span>Enabled</span>
          </label>
        </div>
        <p className="field-hint" style={{ marginBottom: 14 }}>
          Every log line at or above the level below -- the application log and the authentication / RBAC audit
          trail -- is batched and sent to the platform you use. Delivery is best effort and never blocks the app;
          counters below show what got through.
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
        <div className="dest-picker" role="radiogroup" aria-label="Log destination">
          {(Object.keys(meta.destinations) as LogDestination[]).map((d) => (
            <button
              key={d}
              type="button"
              role="radio"
              aria-checked={cfg.destination === d}
              className={`dest-option${cfg.destination === d ? " active" : ""}`}
              onClick={() => chooseDestination(d)}
            >
              {meta.destinations[d].label}
            </button>
          ))}
        </div>
        <div className="form-grid" style={{ marginTop: 14 }}>
          {dest?.fields.map((f) => (
            <div className="field" key={f.key}>
              <label htmlFor={`lf-${f.key}`}>
                {f.label}
                {f.required && " *"}
              </label>
              {f.options ? (
                <select id={`lf-${f.key}`} value={cfg.settings[f.key] ?? f.default ?? ""} onChange={(e) => setField(f.key, e.target.value)}>
                  {f.options.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id={`lf-${f.key}`}
                  type={f.secret ? "password" : "text"}
                  autoComplete={f.secret ? "new-password" : "off"}
                  placeholder={
                    f.secret && cfg.secrets_set?.includes(f.key) ? "•••••••• (stored; leave blank to keep)" : f.placeholder ?? f.default ?? ""
                  }
                  value={cfg.settings[f.key] ?? ""}
                  onChange={(e) => setField(f.key, e.target.value)}
                />
              )}
              {f.hint && <span className="field-hint">{f.hint}</span>}
              {f.secret && <span className="field-hint">Stored encrypted with the state encryption key.</span>}
            </div>
          ))}
        </div>
        {cfg.destination === "syslog" && cfg.settings.protocol === "tls" && (
          <p className="field-hint" style={{ marginTop: 8 }}>
            The syslog server's certificate is verified against the public CAs plus any under Certificates.
          </p>
        )}
      </section>

      <section className="card">
        <div className="card-title">What to send</div>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="lf-level">Minimum level</label>
            <select id="lf-level" value={cfg.min_level} onChange={(e) => set("min_level", e.target.value as LogForwardConfig["min_level"])}>
              {meta.levels.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </div>
          <div className="checkbox-row">
            <input id="lf-audit" type="checkbox" checked={cfg.include_audit} onChange={(e) => set("include_audit", e.target.checked)} />
            <label htmlFor="lf-audit">
              Authentication &amp; RBAC audit trail <span className="field-hint">(logins, lockouts, user/role/setting changes)</span>
            </label>
          </div>
          <div className="checkbox-row">
            <input id="lf-app" type="checkbox" checked={cfg.include_app} onChange={(e) => set("include_app", e.target.checked)} />
            <label htmlFor="lf-app">
              Application log <span className="field-hint">(migrations, sync, validation, errors)</span>
            </label>
          </div>
          <div className="form-grid-2">
            <div className="field">
              <label htmlFor="lf-batch">Batch size</label>
              <input id="lf-batch" type="number" min={1} max={1000} value={cfg.batch_size} onChange={(e) => set("batch_size", Number(e.target.value))} />
            </div>
            <div className="field">
              <label htmlFor="lf-flush">Flush every (seconds)</label>
              <input id="lf-flush" type="number" min={1} max={300} value={cfg.flush_interval_s} onChange={(e) => set("flush_interval_s", Number(e.target.value))} />
            </div>
          </div>
          <div className="field">
            <label htmlFor="lf-host">Hostname to report</label>
            <input id="lf-host" placeholder="(container hostname)" value={cfg.hostname} onChange={(e) => set("hostname", e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="lf-extra">Extra fields on every event</label>
            <input id="lf-extra" className="mono" placeholder="env=prod, site=hal" value={extraText} onChange={(e) => setExtraText(e.target.value)} />
            <span className="field-hint">key=value pairs, comma-separated.</span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-title-row">
          <div className="card-title">Delivery</div>
          {status && (
            <span className={`badge ${status.active ? (status.last_error ? "badge-warning" : "badge-success") : "badge-neutral"}`}>
              {status.active ? (status.last_error ? "errors" : "forwarding") : "off"}
            </span>
          )}
        </div>
        {status && (
          <div className="kv-grid" style={{ marginBottom: 12 }}>
            <div className="kv-k">Sent</div>
            <div>{status.sent.toLocaleString()} events</div>
            <div className="kv-k">Failed batches</div>
            <div>{status.failed}</div>
            <div className="kv-k">Dropped</div>
            <div>
              {status.dropped} <span className="field-hint">(queue overflow)</span>
            </div>
            <div className="kv-k">Queued</div>
            <div>{status.queued}</div>
            <div className="kv-k">Last success</div>
            <div className="field-hint">{status.last_success ? new Date(status.last_success).toLocaleString() : "-"}</div>
            <div className="kv-k">Last error</div>
            <div className="field-hint" style={{ color: status.last_error ? "var(--red)" : undefined }}>
              {status.last_error ?? "-"}
            </div>
          </div>
        )}
        <div className="btn-wrap">
          <button type="button" className="btn btn-secondary" disabled={busy !== null} onClick={test}>
            {busy === "test" ? "Sending…" : "Send test event"}
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy !== null}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
        </div>
      </section>
    </form>
  );
}
