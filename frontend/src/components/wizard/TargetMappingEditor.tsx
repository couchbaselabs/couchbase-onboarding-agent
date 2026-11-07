import { Plus, X } from "lucide-react";
import type { ContainerMigrationSpec, SourceTopologySnapshot, TargetKeyspace } from "@/api/types";
import { useWizardStore } from "@/store/wizardStore";

/** Where each source container lands: one or more bucket / scope / collection
 * targets per container. Blank fields use the plan's defaults (the destination
 * bucket; a scope named after the source database, or `_default`; a collection
 * named after the container). The first target is the primary copy. */
export default function TargetMappingEditor({
  containers,
  topology,
  defaultBucket,
  knownBuckets,
  isCouchbaseSource,
}: {
  containers: ContainerMigrationSpec[];
  topology: SourceTopologySnapshot | null;
  defaultBucket: string;
  knownBuckets: string[];
  isCouchbaseSource: boolean;
}) {
  const toggle = useWizardStore((s) => s.toggleContainer);
  const add = useWizardStore((s) => s.addContainerTarget);
  const update = useWizardStore((s) => s.updateContainerTarget);
  const remove = useWizardStore((s) => s.removeContainerTarget);

  const bucketOptions = Array.from(new Set([defaultBucket, ...knownBuckets].filter(Boolean)));
  const defaultScope = isCouchbaseSource ? "" : "source database name or _default";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <datalist id="target-bucket-options">
        {bucketOptions.map((b) => (
          <option key={b} value={b} />
        ))}
      </datalist>
      {isCouchbaseSource && (
        <div className="muted">
          Couchbase sources keep their own scope.collection names (cbbackupmgr and XDCR move the bucket structure as-is);
          only the target bucket can be changed per target.
        </div>
      )}
      <div style={{ maxHeight: 420, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }} className="cb-scrollbar">
        {containers.map((c) => {
          const stats = topology?.containers.find((sc) => sc.name === c.container_name);
          const targets = c.targets?.length ? c.targets : [{ bucket: null, scope: null, collection: null }];
          return (
            <div
              key={c.container_name}
              style={{
                border: "1px solid var(--border)", borderRadius: 8, padding: "8px 10px",
                opacity: c.include ? 1 : 0.55,
              }}
            >
              <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 12.5 }}>
                <input type="checkbox" checked={c.include} onChange={() => toggle(c.container_name)} />
                <span style={{ fontWeight: 600 }}>{c.container_name}</span>
                <span style={{ color: "var(--text-faint)", marginLeft: "auto" }}>
                  {stats?.estimated_count?.toLocaleString() ?? "?"} docs
                  {targets.length > 1 ? ` · ${targets.length} targets` : ""}
                </span>
              </label>
              {c.include && (
                <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8 }}>
                  {targets.map((t, i) => (
                    <TargetRow
                      key={i}
                      index={i}
                      target={t}
                      primary={i === 0}
                      canRemove={targets.length > 1}
                      defaultBucket={defaultBucket}
                      defaultScope={defaultScope}
                      defaultCollection={c.container_name}
                      lockNames={isCouchbaseSource}
                      onChange={(patch) => update(c.container_name, i, patch)}
                      onRemove={() => remove(c.container_name, i)}
                    />
                  ))}
                  <button
                    type="button"
                    className="btn btn-secondary"
                    style={{ alignSelf: "flex-start", padding: "3px 8px", fontSize: 11.5 }}
                    onClick={() => add(c.container_name)}
                  >
                    <Plus size={12} /> Add target
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TargetRow({
  index,
  target,
  primary,
  canRemove,
  defaultBucket,
  defaultScope,
  defaultCollection,
  lockNames,
  onChange,
  onRemove,
}: {
  index: number;
  target: TargetKeyspace;
  primary: boolean;
  canRemove: boolean;
  defaultBucket: string;
  defaultScope: string;
  defaultCollection: string;
  lockNames: boolean;
  onChange: (patch: Partial<TargetKeyspace>) => void;
  onRemove: () => void;
}) {
  const field = (
    key: keyof TargetKeyspace,
    placeholder: string,
    disabled = false,
  ) => (
    <input
      value={target[key] ?? ""}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={`Target ${index + 1} ${key}`}
      onChange={(e) => onChange({ [key]: e.target.value || null })}
      style={{ flex: 1, minWidth: 0, fontSize: 12 }}
      list={key === "bucket" ? "target-bucket-options" : undefined}
    />
  );
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span
        className={`badge ${primary ? "badge-info" : "badge-neutral"}`}
        title={primary ? "Primary copy: verification, reconciliation and fallback use this keyspace." : "Secondary copy: receives the same writes."}
        style={{ minWidth: 64, justifyContent: "center" }}
      >
        {primary ? "primary" : `copy ${index}`}
      </span>
      {field("bucket", defaultBucket || "bucket")}
      <span style={{ color: "var(--text-faint)" }}>.</span>
      {field("scope", lockNames ? "(source scope)" : defaultScope, lockNames)}
      <span style={{ color: "var(--text-faint)" }}>.</span>
      {field("collection", lockNames ? "(source collection)" : defaultCollection, lockNames)}
      <button
        type="button"
        className="btn btn-secondary"
        onClick={onRemove}
        disabled={!canRemove}
        aria-label="Remove target"
        title={canRemove ? "Remove this target" : "A container needs at least one target"}
        style={{ padding: "3px 6px" }}
      >
        <X size={12} />
      </button>
    </div>
  );
}
