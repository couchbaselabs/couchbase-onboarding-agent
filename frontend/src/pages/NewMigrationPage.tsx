import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useWizardStore } from "@/store/wizardStore";
import StepIndicator from "@/components/wizard/StepIndicator";
import SourceConfigForm from "@/components/wizard/SourceConfigForm";
import CouchbaseConfigForm from "@/components/wizard/CouchbaseConfigForm";
import ReplicationModeSelector from "@/components/wizard/ReplicationModeSelector";
import ValidationResults from "@/components/validation/ValidationResults";
import type { MigrationStrategy, ReplicationModeRecommendationResponse, ValidationReport } from "@/api/types";

function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      style={{
        background: "rgba(255,75,79,0.1)", border: "1px solid var(--cb-red-bright)",
        borderRadius: 8, padding: "10px 14px", fontSize: 12.5, color: "var(--cb-red-bright)",
        display: "flex", gap: 8, alignItems: "flex-start",
      }}
    >
      <XCircle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
      <span>{message}</span>
    </div>
  );
}

export default function NewMigrationPage() {
  const navigate = useNavigate();
  const wizard = useWizardStore();
  const [testingSource, setTestingSource] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [testingDest, setTestingDest] = useState(false);
  const [destError, setDestError] = useState<string | null>(null);
  const [cutoverPlan, setCutoverPlan] = useState<"cutover" | "phased" | null>(null);
  const [recommendation, setRecommendation] = useState<ReplicationModeRecommendationResponse | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [migrationId, setMigrationId] = useState<string | null>(null);
  const [validationReport, setValidationReport] = useState<ValidationReport | null>(null);
  const [approving, setApproving] = useState(false);
  const [approvedBy, setApprovedBy] = useState("");

  async function testSource() {
    setTestingSource(true);
    setSourceError(null);
    try {
      const topo = await api.testSourceConnection(wizard.source);
      wizard.setSourceTopology(topo);
    } catch (err) {
      setSourceError(err instanceof ApiError ? err.message : "Could not reach the backend.");
      wizard.setSourceTopology(null);
    } finally {
      setTestingSource(false);
    }
  }

  async function testDestination() {
    setTestingDest(true);
    setDestError(null);
    try {
      const topo = await api.testDestinationConnection(wizard.destination);
      wizard.setDestTopology(topo);
    } catch (err) {
      setDestError(err instanceof ApiError ? err.message : "Could not reach the backend.");
      wizard.setDestTopology(null);
    } finally {
      setTestingDest(false);
    }
  }

  async function askAgent(plan: "cutover" | "phased") {
    setCutoverPlan(plan);
    if (!wizard.sourceTopology) return;
    try {
      const rec = await api.recommendMode(plan, wizard.sourceTopology, wizard.concurrency);
      setRecommendation(rec);
    } catch {
      setRecommendation(null);
    }
  }

  async function createAndValidate() {
    setCreating(true);
    setCreateError(null);
    try {
      const record = await api.createMigration({
        name: wizard.name,
        source: wizard.source,
        destination: wizard.destination,
        destination_bucket: wizard.destinationBucket,
        destination_bucket_ram_quota_mb: wizard.destinationBucketRamQuotaMb,
        strategy: wizard.strategy,
        containers: wizard.containers,
        concurrency: wizard.concurrency,
      });
      setMigrationId(record.migration_id);
      const report = await api.validateMigration(record.migration_id);
      setValidationReport(report);
      wizard.setStep(2);
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Could not create the migration.");
    } finally {
      setCreating(false);
    }
  }

  async function approve() {
    if (!migrationId) return;
    setApproving(true);
    try {
      await api.approveMigration(migrationId, approvedBy || "operator");
      navigate(`/migrations/${migrationId}`);
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Could not approve the migration.");
    } finally {
      setApproving(false);
    }
  }

  const cdcDisabled: MigrationStrategy[] = wizard.sourceTopology?.supports_cdc ? [] : ["cdc_live", "full_load_and_cdc"];

  return (
    <div style={{ padding: 32, maxWidth: 760 }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 4 }}>New migration</h1>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, marginBottom: 24 }}>
        Move data from a source database into Couchbase Server or Couchbase Capella.
      </p>
      <StepIndicator step={wizard.step} />

      {wizard.step === 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="cb-card">
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 5 }}>
              Migration name
            </div>
            <input
              value={wizard.name}
              onChange={(e) => wizard.setName(e.target.value)}
              placeholder="e.g. Orders MongoDB -> Capella"
              style={{ width: "100%" }}
            />
          </div>

          <div className="cb-card">
            <SourceConfigForm value={wizard.source} onChange={wizard.setSource} />
          </div>

          {sourceError && <ErrorBanner message={sourceError} />}

          {wizard.sourceTopology && (
            <div
              className="cb-badge"
              style={{ background: "rgba(46,204,113,0.12)", color: "var(--cb-green)", width: "fit-content" }}
            >
              <CheckCircle2 size={13} /> Connected &middot; {wizard.sourceTopology.containers.length} container(s) &middot;{" "}
              {wizard.sourceTopology.server_edition || wizard.sourceTopology.server_version || "unknown version"}
            </div>
          )}

          <div style={{ display: "flex", gap: 10 }}>
            <button className="cb-btn" onClick={testSource} disabled={testingSource}>
              {testingSource ? <Loader2 size={14} className="cb-spin" /> : null} Test &amp; introspect source
            </button>
            <button
              className="cb-btn cb-btn-primary"
              disabled={!wizard.sourceTopology || !wizard.name.trim()}
              onClick={() => wizard.setStep(1)}
              style={{ marginLeft: "auto" }}
            >
              Next
            </button>
          </div>
        </div>
      )}

      {wizard.step === 1 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="cb-card">
            <CouchbaseConfigForm value={wizard.destination} onChange={wizard.setDestination} />
          </div>
          {destError && <ErrorBanner message={destError} />}
          {wizard.destTopology && (
            <div
              className="cb-badge"
              style={{ background: "rgba(46,204,113,0.12)", color: "var(--cb-green)", width: "fit-content" }}
            >
              <CheckCircle2 size={13} /> Reachable &middot; Couchbase {wizard.destTopology.cluster_version || "unknown"}
            </div>
          )}
          <div className="cb-card" style={{ display: "flex", gap: 12, alignItems: "flex-end" }}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 5 }}>
                Destination bucket
              </div>
              <input
                value={wizard.destinationBucket}
                onChange={(e) => wizard.setDestinationBucket(e.target.value)}
                placeholder="e.g. orders"
                style={{ width: "100%" }}
              />
            </div>
            <button className="cb-btn" onClick={testDestination} disabled={testingDest}>
              Test destination connection
            </button>
          </div>

          {wizard.sourceTopology && (
            <div className="cb-card">
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Ask the agent: which replication mode fits?</div>
              {!recommendation && (
                <div style={{ display: "flex", gap: 10 }}>
                  <button className="cb-btn" onClick={() => askAgent("cutover")}>
                    All applications cut over at once
                  </button>
                  <button className="cb-btn" onClick={() => askAgent("phased")}>
                    Applications migrate gradually
                  </button>
                </div>
              )}
              {recommendation && (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "var(--cb-teal)" }}>{recommendation.headline}</div>
                  <div style={{ fontSize: 12.5, color: "var(--text-secondary)" }}>{recommendation.rationale}</div>
                  {recommendation.considerations.map((c, i) => (
                    <div key={i} style={{ fontSize: 12, color: "var(--text-muted)" }}>
                      &bull; {c}
                    </div>
                  ))}
                  <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
                    <button
                      className="cb-btn cb-btn-primary"
                      onClick={() => wizard.setStrategy(recommendation.recommended_strategy)}
                    >
                      Use this mode
                    </button>
                    <button className="cb-btn" onClick={() => cutoverPlan && askAgent(cutoverPlan)}>
                      Ask again
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="cb-card">
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Replication mode</div>
            <ReplicationModeSelector value={wizard.strategy} onChange={wizard.setStrategy} disabledValues={cdcDisabled} />
            {cdcDisabled.length > 0 && wizard.sourceTopology?.cdc_notes && (
              <div style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 8 }}>
                {wizard.sourceTopology.cdc_notes}
              </div>
            )}
          </div>

          {wizard.sourceTopology && (
            <div className="cb-card">
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Containers to migrate</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 260, overflowY: "auto" }} className="cb-scrollbar">
                {wizard.containers.map((c) => (
                  <label key={c.container_name} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12.5 }}>
                    <input type="checkbox" checked={c.include} onChange={() => wizard.toggleContainer(c.container_name)} />
                    <span style={{ fontWeight: 600 }}>{c.container_name}</span>
                    <span style={{ color: "var(--text-muted)", marginLeft: "auto" }}>
                      {wizard.sourceTopology?.containers.find((sc) => sc.name === c.container_name)?.estimated_count?.toLocaleString() ?? "?"} docs
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="cb-card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>Concurrency</div>
            <input
              type="number"
              min={1}
              max={64}
              value={wizard.concurrency}
              onChange={(e) => wizard.setConcurrency(Number(e.target.value))}
              style={{ width: 80 }}
            />
            <div style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              Parallel workers writing to Couchbase; auto-throttled down if the agent detects backpressure.
            </div>
          </div>

          {createError && <ErrorBanner message={createError} />}

          <div style={{ display: "flex", gap: 10 }}>
            <button className="cb-btn" onClick={() => wizard.setStep(0)}>
              Back
            </button>
            <button
              className="cb-btn cb-btn-primary"
              style={{ marginLeft: "auto" }}
              disabled={!wizard.destinationBucket.trim() || creating}
              onClick={createAndValidate}
            >
              {creating ? "Validating..." : "Create & validate"}
            </button>
          </div>
        </div>
      )}

      {wizard.step === 2 && validationReport && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <ValidationResults report={validationReport} />
          {!validationReport.passed && <ErrorBanner message="One or more required checks failed -- fix the issue above, go back, and re-create the migration." />}
          <div style={{ display: "flex", gap: 10 }}>
            <button className="cb-btn" onClick={() => wizard.setStep(1)}>
              Back
            </button>
            <button
              className="cb-btn cb-btn-primary"
              style={{ marginLeft: "auto" }}
              disabled={!validationReport.passed}
              onClick={() => wizard.setStep(3)}
            >
              Continue
            </button>
          </div>
        </div>
      )}

      {wizard.step === 3 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="cb-card">
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 10 }}>Summary</div>
            <SummaryRow label="Name" value={wizard.name} />
            <SummaryRow label="Source" value={`${wizard.source.label} (${wizard.source.source_type})`} />
            <SummaryRow label="Destination bucket" value={`${wizard.destination.label} / ${wizard.destinationBucket}`} />
            <SummaryRow label="Replication mode" value={wizard.strategy.replace(/_/g, " ")} />
            <SummaryRow label="Containers" value={`${wizard.containers.filter((c) => c.include).length} included`} />
          </div>
          <div className="cb-card">
            <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 5 }}>Approved by</div>
            <input value={approvedBy} onChange={(e) => setApprovedBy(e.target.value)} placeholder="Your name" style={{ width: "100%" }} />
          </div>
          {createError && <ErrorBanner message={createError} />}
          <div style={{ display: "flex", gap: 10 }}>
            <button className="cb-btn" onClick={() => wizard.setStep(2)}>
              Back
            </button>
            <button className="cb-btn cb-btn-primary" style={{ marginLeft: "auto" }} onClick={approve} disabled={approving}>
              Approve &amp; view migration
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", fontSize: 12.5 }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value}</span>
    </div>
  );
}
