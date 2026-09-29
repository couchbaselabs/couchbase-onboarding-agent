/** A metric tile, as in the Agent Operations Manager: uppercase label, large
 * value, optional hint. `inset` is for tiles shown inside another card. */
export default function StatCard({
  label,
  value,
  sub,
  inset = false,
  valueColor,
}: {
  label: string;
  value: string | number;
  sub?: string;
  inset?: boolean;
  valueColor?: string;
}) {
  return (
    <div className={`stat-card${inset ? " inset" : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={valueColor ? { color: valueColor } : undefined}>
        {value}
      </div>
      {sub && <div className="stat-hint">{sub}</div>}
    </div>
  );
}
