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
    description: "Change-data-capture starts immediately and stays running until you stop it.",
  },
  {
    value: "full_load_and_cdc",
    label: "Bulk copy + continuous sync",
    description: "A full load for existing data, then change-data-capture takes over for the ongoing delta.",
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
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {OPTIONS.map((opt) => {
        const disabled = disabledValues?.includes(opt.value);
        const selected = value === opt.value;
        return (
          <label
            key={opt.value}
            className="cb-card"
            style={{
              padding: "12px 14px",
              display: "flex",
              gap: 12,
              alignItems: "flex-start",
              cursor: disabled ? "not-allowed" : "pointer",
              opacity: disabled ? 0.45 : 1,
              borderColor: selected ? "var(--cb-teal)" : "var(--border-subtle)",
            }}
          >
            <input
              type="radio"
              checked={selected}
              disabled={disabled}
              onChange={() => onChange(opt.value)}
              style={{ marginTop: 3 }}
            />
            <div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{opt.label}</div>
              <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 2 }}>{opt.description}</div>
            </div>
          </label>
        );
      })}
    </div>
  );
}
