import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { CertInfo, ServingCertStatus, TrustedCerts } from "@/api/types";
import PemInput from "@/components/settings/PemInput";

/** Settings -> Certificates: the CAs this app trusts when it connects out, and
 * the certificate its own HTTPS UI serves. */
export default function CertificateSettings() {
  return (
    <div className="stack">
      <TrustedCas />
      <ServingCertificate />
    </div>
  );
}

function expiry(c: CertInfo) {
  const d = new Date(c.not_after);
  const days = Math.round((d.getTime() - Date.now()) / 86_400_000);
  if (c.expired) return <span className="badge badge-danger">expired {d.toLocaleDateString()}</span>;
  if (days < 30) return <span className="badge badge-warning">expires in {days} d</span>;
  return <span className="field-hint">expires {d.toLocaleDateString()}</span>;
}

// ---------------------------------------------------------------- trusted CAs

function TrustedCas() {
  const [data, setData] = useState<TrustedCerts | null>(null);
  const [pem, setPem] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await api.trustedCerts());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load the trust store.");
    }
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <section className="card">
      <div className="card-title">Trusted CAs (outbound)</div>
      <p className="field-hint" style={{ marginBottom: 14 }}>
        Certificate authorities this app trusts <em>in addition to</em> the public ones when it connects out: an
        LDAPS directory, Couchbase or Capella clusters behind a private CA, a hosted LLM endpoint through a
        TLS-inspecting proxy. A CA pasted on an individual migration's connection form still applies to that
        connection only. Changes take effect for new connections immediately.
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
      {data && data.certificates.length > 0 ? (
        <div className="table-wrap" style={{ marginBottom: 14 }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Subject</th>
                <th>Issuer</th>
                <th>Validity</th>
                <th>SHA-256</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.certificates.map((c) => (
                <tr key={c.fingerprint}>
                  <td>
                    <strong>{c.subject}</strong>
                    {c.self_signed && <span className="badge badge-neutral" style={{ marginLeft: 8 }}>root</span>}
                  </td>
                  <td className="field-hint">{c.issuer}</td>
                  <td>{expiry(c)}</td>
                  <td className="field-hint mono" title={c.fingerprint}>
                    {c.fingerprint.slice(0, 16)}…
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={async () => {
                        if (!window.confirm(`Stop trusting "${c.subject}"?`)) return;
                        try {
                          await api.removeTrustedCert(c.fingerprint);
                          setNotice(`Removed ${c.subject}.`);
                          reload();
                        } catch (err) {
                          setError(err instanceof ApiError ? err.message : "Couldn't remove it.");
                        }
                      }}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        data && <p className="field-hint" style={{ marginBottom: 14 }}>No custom CAs yet -- only the public roots are trusted.</p>
      )}
      <form
        className="form-grid"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          setNotice(null);
          try {
            const r = await api.addTrustedCert(pem);
            setNotice(r.added.length ? `Added ${r.added.length} certificate(s).` : "Already trusted -- nothing added.");
            setPem("");
            reload();
          } catch (err) {
            setError(err instanceof ApiError ? err.message : "Couldn't add the certificate.");
          } finally {
            setBusy(false);
          }
        }}
      >
        <PemInput
          label="CA certificate (PEM)"
          value={pem}
          onChange={setPem}
          placeholder={"-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----"}
          hint="A bundle with several CAs is fine; each is listed separately. Server (leaf) certificates are refused."
        />
        <div className="btn-wrap">
          <button className="btn btn-primary" type="submit" disabled={busy || !pem.includes("BEGIN CERTIFICATE")}>
            {busy ? "Adding…" : "Add to trust store"}
          </button>
        </div>
      </form>
    </section>
  );
}

// ---------------------------------------------------------------- serving cert

function ServingCertificate() {
  const [status, setStatus] = useState<ServingCertStatus | null>(null);
  const [cert, setCert] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setStatus(await api.servingCert());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load the certificate status.");
    }
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);

  const publish = status?.publish;

  return (
    <section className="card">
      <div className="card-title">This UI's HTTPS certificate</div>
      <p className="field-hint" style={{ marginBottom: 14 }}>
        The certificate the Onboarding Agent presents at <code>{window.location.origin}</code>. Out of the box it's
        self-signed (hence the browser warning). Upload one issued by your CA -- the server certificate first, then
        its intermediate chain -- together with its unencrypted private key. nginx reloads it within about a minute;
        no restart.
      </p>
      {publish?.mode === "none" && (
        <div className="helper-banner helper-banner-neutral" style={{ marginBottom: 10 }}>
          {publish.note}
        </div>
      )}
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
      {status?.installed && status.certificate && (
        <div className="kv-grid" style={{ marginBottom: 14 }}>
          <div className="kv-k">Subject</div>
          <div>
            <strong>{status.certificate.subject}</strong> {expiry(status.certificate)}
          </div>
          <div className="kv-k">Names</div>
          <div className="field-hint">{status.certificate.sans.join(", ") || "-"}</div>
          <div className="kv-k">Issuer</div>
          <div className="field-hint">
            {status.certificate.issuer}
            {status.chain.length > 0 && ` (+${status.chain.length} in chain)`}
          </div>
          <div className="kv-k">Published to</div>
          <div className="field-hint">
            {publish?.mode === "kubernetes-secret" ? `Kubernetes Secret ${publish.target}` : publish?.target ?? "-"}
            {status.installed_at && ` · installed ${new Date(status.installed_at).toLocaleString()}`}
          </div>
        </div>
      )}
      {status && !status.installed && (
        <p className="field-hint" style={{ marginBottom: 14 }}>
          No uploaded certificate -- the UI serves the one generated at start (or the one mounted into the frontend).
        </p>
      )}
      <form
        className="form-grid"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          setNotice(null);
          try {
            const r = await api.installServingCert(cert, key);
            setNotice(`Installed ${r.certificate?.subject}. Published to ${r.published}.`);
            setCert("");
            setKey("");
            setStatus(r);
          } catch (err) {
            setError(err instanceof ApiError ? err.message : "Couldn't install the certificate.");
          } finally {
            setBusy(false);
          }
        }}
      >
        <PemInput
          label="Certificate (PEM, leaf first, then the chain)"
          value={cert}
          onChange={setCert}
          placeholder={"-----BEGIN CERTIFICATE-----\n…server…\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\n…intermediate…\n-----END CERTIFICATE-----"}
        />
        <PemInput
          label="Private key (PEM, no passphrase)"
          value={key}
          onChange={setKey}
          rows={4}
          placeholder={"-----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----"}
          hint="Sent once over this HTTPS session and stored with mode 600 on the state volume."
        />
        <div className="btn-wrap">
          <button
            className="btn btn-primary"
            type="submit"
            disabled={busy || !cert.includes("BEGIN CERTIFICATE") || !key.includes("PRIVATE KEY")}
          >
            {busy ? "Installing…" : "Install certificate"}
          </button>
          {status?.installed && (
            <button
              className="btn btn-secondary"
              type="button"
              disabled={busy}
              onClick={async () => {
                if (!window.confirm("Remove the uploaded certificate? The UI falls back to a self-signed one on the next frontend restart.")) return;
                try {
                  const r = await api.removeServingCert();
                  setStatus(r);
                  setNotice(r.note ?? "Removed.");
                } catch (err) {
                  setError(err instanceof ApiError ? err.message : "Couldn't remove it.");
                }
              }}
            >
              Remove
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
