import type { MigrationStrategy } from "@/api/types";

const OPTIONS: { value: MigrationStrategy; label: string; description: string }[] = [
  {
    value: "full_load",
    label: "One-time migration",
    description: "A single full-load pass. The migration finishes once the transfer completes.",
  },
  {
    value: "cdc_live",
    label: "Continuous replication",
    description:
      "No separate bulk copy: change capture starts from the beginning of the change log (or now, where the log has no history) and runs until cutover.",
  },
  {
    value: "full_load_and_cdc",
    label: "Bulk copy + continuous sync",
    description:
      "Records the source's change position, copies existing data, then replays every change made since -- no gap -- and keeps syncing until cutover.",
  },
];

export default function ReplicationModeSelector({
  value,
  onChange,
  disabledValues,
}: {
  value: MigrationStrategy;
  onChange: (v: MigrationStrategy) => void;
  disabledValues?: MigrationStrategy[];
}) {
  return (
    <div role="radiogroup" aria-label="Replication mode" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {OPTIONS.map((opt) => {
        const disabled = disabledValues?.includes(opt.value);
        const selected = value === opt.value;
        return (
          <label key={opt.value} className={`choice${selected ? " selected" : ""}${disabled ? " disabled" : ""}`}>
            <input
              type="radio"
              name="replication-mode"
              checked={selected}
              disabled={disabled}
              onChange={() => onChange(opt.value)}
            />
            <div>
              <div className="choice-title">{opt.label}</div>
              <div className="choice-desc">{opt.description}</div>
            </div>
          </label>
        );
      })}
    </div>
  );
}
