export function CouchbaseWordmark() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div
        style={{
          width: 34, height: 34, borderRadius: 9,
          background: "linear-gradient(135deg, var(--cb-red), var(--cb-red-bright))",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontWeight: 800, fontSize: 17, color: "white",
        }}
      >
        C
      </div>
      <div style={{ lineHeight: 1.1 }}>
        <div style={{ fontWeight: 700, fontSize: 22, color: "var(--text-primary)" }}>Couchbase</div>
        <div style={{ fontSize: 22, color: "var(--text-primary)", fontWeight: 700, letterSpacing: 0.02 }}>
          Onboarding Agent
        </div>
      </div>
    </div>
  );
}
