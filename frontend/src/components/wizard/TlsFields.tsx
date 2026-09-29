/** CA certificate + verification controls shared by the source and destination
 * forms. Verification is on by default; skipping it is for throwaway test
 * clusters and the validator flags it. */
export default function TlsFields({
  caPem,
  skipVerify,
  onChange,
}: {
  caPem?: string;
  skipVerify?: boolean;
  onChange: (patch: { ca_cert_pem?: string; tls_skip_verify?: boolean }) => void;
}) {
  return (
    <details style={{ fontSize: 12.5 }}>
      <summary style={{ cursor: "pointer", color: "var(--text-secondary)", fontWeight: 600 }}>
        TLS certificate options
      </summary>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>
            CA certificate (PEM, optional)
          </span>
          <textarea
            rows={4}
            value={caPem || ""}
            onChange={(e) => onChange({ ca_cert_pem: e.target.value })}
            placeholder="-----BEGIN CERTIFICATE-----"
            style={{ fontFamily: "ui-monospace, monospace", fontSize: 11.5 }}
          />
          <span className="cb-muted">
            Paste the CA that signed the server certificate when it isn't publicly trusted (self-signed or private
            CA, Capella's management endpoint).
          </span>
        </label>
        <label style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <input
            type="checkbox"
            style={{ marginTop: 2 }}
            checked={!!skipVerify}
            onChange={(e) => onChange({ tls_skip_verify: e.target.checked })}
          />
          <span>
            Skip certificate verification
            <div className="cb-muted" style={{ marginTop: 2 }}>
              Test clusters only -- the connection is still encrypted but the server isn't authenticated.
            </div>
          </span>
        </label>
      </div>
    </details>
  );
}
