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
import CdcCapabilities from "@/components/wizard/CdcCapabilities";
import type { MigrationStrategy, ReplicationModeRecommendationResponse, ValidationReport } from "@/api/types";
import { SOURCE_TYPE_LABELS } from "@/theme/tokens";

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="helper-banner helper-banner-danger" role="alert">
      <XCircle size={15} />
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
        reconcile_interval_minutes: wizard.reconcileIntervalMinutes,
        journal_overwrites: wizard.journalOverwrites,
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
    <div className="page-narrow">
      <div className="page-header">
        <div>
          <h1 className="page-title">New migration</h1>
          <p className="page-subtitle">Move data from a source database into Couchbase Server or Couchbase Capella.</p>
        </div>
      </div>
      <StepIndicator step={wizard.step} />

      {wizard.step === 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card field">
            <label htmlFor="migration-name">Migration name</label>
            <input
              id="migration-name"
              value={wizard.name}
              onChange={(e) => wizard.setName(e.target.value)}
              placeholder="e.g. Orders MongoDB -> Capella"
              style={{ width: "100%" }}
            />
          </div>

          <div className="card">
            <h3 className="card-title">Source database</h3>
            <SourceConfigForm value={wizard.source} onChange={wizard.setSource} />
          </div>

          {sourceError && <ErrorBanner message={sourceError} />}

          {wizard.sourceTopology && (
            <div className="helper-banner helper-banner-success" role="status" style={{ width: "fit-content" }}>
              <CheckCircle2 size={13} /> Connected &middot; {wizard.sourceTopology.containers.length} container(s) &middot;{" "}
              {wizard.sourceTopology.server_edition || wizard.sourceTopology.server_version || "unknown version"}
            </div>
          )}
          {wizard.sourceTopology && <CdcCapabilities topology={wizard.sourceTopology} />}

          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn btn-secondary" onClick={testSource} disabled={testingSource}>
              {testingSource ? <Loader2 size={14} className="cb-spin" /> : null} Test &amp; introspect source
            </button>
            <button
              className="btn btn-primary"
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
          <div className="card">
            <h3 className="card-title">Destination cluster</h3>
            <CouchbaseConfigForm value={wizard.destination} onChange={wizard.setDestination} />
          </div>
          {destError && <ErrorBanner message={destError} />}
          {wizard.destTopology && (
            <div className="helper-banner helper-banner-success" role="status" style={{ width: "fit-content" }}>
              <CheckCircle2 size={13} /> Reachable &middot; Couchbase {wizard.destTopology.cluster_version || "unknown"}
            </div>
          )}
          <div className="card" style={{ display: "flex", gap: 12, alignItems: "flex-end" }}>
            <div style={{ flex: 1 }}>
              <label className="field-label" htmlFor="dest-bucket" style={{ marginBottom: 6 }}>
                Destination bucket
              </label>
              <input
                id="dest-bucket"
                value={wizard.destinationBucket}
                onChange={(e) => wizard.setDestinationBucket(e.target.value)}
                placeholder="e.g. orders"
                style={{ width: "100%" }}
              />
            </div>
            <button className="btn btn-secondary" onClick={testDestination} disabled={testingDest}>
              Test destination connection
            </button>
          </div>

          {wizard.sourceTopology && (
            <div className="card">
              <h3 className="card-title">Ask the agent: which replication mode fits?</h3>
              {!recommendation && (
                <div style={{ display: "flex", gap: 10 }}>
                  <button className="btn btn-secondary" onClick={() => askAgent("cutover")}>
                    All applications cut over at once
                  </button>
                  <button className="btn btn-secondary" onClick={() => askAgent("phased")}>
                    Applications migrate gradually
                  </button>
                </div>
              )}
              {recommendation && (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span className="badge badge-info">Recommendation</span>
                    <span style={{ fontSize: 13.5, fontWeight: 700 }}>{recommendation.headline}</span>
                  </div>
                  <div style={{ fontSize: 12.5, color: "var(--text-muted)" }}>{recommendation.rationale}</div>
                  {recommendation.considerations.map((c, i) => (
                    <div key={i} style={{ fontSize: 12, color: "var(--text-faint)" }}>
                      &bull; {c}
                    </div>
                  ))}
                  <div style={{ display: "flex", gap: 10, marginTop: 6 }}>
                    <button
                      className="btn btn-primary"
                      onClick={() => wizard.setStrategy(recommendation.recommended_strategy)}
                    >
                      Use this mode
                    </button>
                    <button className="btn btn-secondary" onClick={() => cutoverPlan && askAgent(cutoverPlan)}>
                      Ask again
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="card">
            <h3 className="card-title">Replication mode</h3>
            <ReplicationModeSelector value={wizard.strategy} onChange={wizard.setStrategy} disabledValues={cdcDisabled} />
            {cdcDisabled.length > 0 && wizard.sourceTopology?.cdc_notes && (
              <div style={{ fontSize: 11.5, color: "var(--text-faint)", marginTop: 8 }}>
                {wizard.sourceTopology.cdc_notes}
              </div>
            )}
          </div>

          {wizard.sourceTopology && (
            <div className="card">
              <h3 className="card-title">Containers to migrate</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 260, overflowY: "auto" }} className="cb-scrollbar">
                {wizard.containers.map((c) => (
                  <label key={c.container_name} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12.5 }}>
                    <input type="checkbox" checked={c.include} onChange={() => wizard.toggleContainer(c.container_name)} />
                    <span style={{ fontWeight: 600 }}>{c.container_name}</span>
                    <span style={{ color: "var(--text-faint)", marginLeft: "auto" }}>
                      {wizard.sourceTopology?.containers.find((sc) => sc.name === c.container_name)?.estimated_count?.toLocaleString() ?? "?"} docs
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          <div className="card" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <h3 className="card-title" style={{ marginBottom: 0 }}>Sync &amp; rollback safety</h3>
            <label style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 12.5 }}>
              <input
                type="checkbox"
                style={{ marginTop: 2 }}
                checked={wizard.journalOverwrites}
                onChange={(e) => wizard.setJournalOverwrites(e.target.checked)}
              />
              <span>
                Journal documents this migration overwrites
                <div className="muted" style={{ marginTop: 2 }}>
                  In destination collections that already exist, the previous version of any document the migration
                  overwrites or deletes is saved first, so rollback can restore it. Collections the migration creates
                  don't need it -- rollback drops them.
                </div>
              </span>
            </label>
            {wizard.strategy !== "full_load" && (
              <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12.5, flexWrap: "wrap" }}>
                <span style={{ fontWeight: 600, color: "var(--text-muted)" }}>Reconcile deletes every</span>
                <input
                  type="number"
                  min={0}
                  max={10080}
                  value={wizard.reconcileIntervalMinutes}
                  onChange={(e) => wizard.setReconcileIntervalMinutes(Math.max(0, Number(e.target.value)))}
                  style={{ width: 90 }}
                  aria-label="Reconciliation interval in minutes"
                />
                <span>minutes (0 = manual only)</span>
                <div className="muted" style={{ flexBasis: "100%" }}>
                  Compares source and destination keys and removes documents deleted at the source.
                  {wizard.sourceTopology && !wizard.sourceTopology.cdc_captures_deletes
                    ? " This source's change capture doesn't report deletes, so keep this on."
                    : " A safety net for changes missed during outages."}
                </div>
              </div>
            )}
          </div>

          <div className="card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <label className="field-label" htmlFor="concurrency">Concurrency</label>
            <input
              id="concurrency"
              type="number"
              min={1}
              max={64}
              value={wizard.concurrency}
              onChange={(e) => wizard.setConcurrency(Number(e.target.value))}
              style={{ width: 80 }}
            />
            <div style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
              Parallel workers writing to Couchbase; auto-throttled down if the agent detects backpressure.
            </div>
          </div>

          {createError && <ErrorBanner message={createError} />}

          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn btn-secondary" onClick={() => wizard.setStep(0)}>
              Back
            </button>
            <button
              className="btn btn-primary"
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
            <button className="btn btn-secondary" onClick={() => wizard.setStep(1)}>
              Back
            </button>
            <button
              className="btn btn-primary"
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
          <div className="card">
            <h3 className="card-title">Summary</h3>
            <table className="data-table">
              <tbody>
            <SummaryRow label="Name" value={wizard.name} />
            <SummaryRow label="Source" value={`${wizard.source.label} (${SOURCE_TYPE_LABELS[wizard.source.source_type] ?? wizard.source.source_type})`} />
            <SummaryRow label="Destination bucket" value={`${wizard.destination.label} / ${wizard.destinationBucket}`} />
            <SummaryRow label="Replication mode" value={MODE_LABELS[wizard.strategy]} />
            {wizard.strategy !== "full_load" && (
              <SummaryRow
                label="Delete reconciliation"
                value={wizard.reconcileIntervalMinutes ? `every ${wizard.reconcileIntervalMinutes} min` : "manual"}
              />
            )}
            <SummaryRow label="Overwrite journal" value={wizard.journalOverwrites ? "on" : "off"} />
            <SummaryRow label="Containers" value={`${wizard.containers.filter((c) => c.include).length} included`} />
              </tbody>
            </table>
          </div>
          <div className="card field">
            <label htmlFor="approved-by">Approved by</label>
            <input id="approved-by" value={approvedBy} onChange={(e) => setApprovedBy(e.target.value)} placeholder="Your name" style={{ width: "100%" }} />
          </div>
          {createError && <ErrorBanner message={createError} />}
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn btn-secondary" onClick={() => wizard.setStep(2)}>
              Back
            </button>
            <button className="btn btn-primary" style={{ marginLeft: "auto" }} onClick={approve} disabled={approving}>
              Approve &amp; view migration
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

const MODE_LABELS: Record<MigrationStrategy, string> = {
  full_load: "One-time migration",
  cdc_live: "Continuous replication",
  full_load_and_cdc: "Bulk copy + continuous sync",
};

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td className="cell-muted" style={{ width: "38%" }}>
        {label}
      </td>
      <td style={{ fontWeight: 600 }}>{value}</td>
    </tr>
  );
}
