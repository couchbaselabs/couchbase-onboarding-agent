import { useEffect, useRef, useState } from "react";
import { Area, AreaChart, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { colors } from "@/theme/tokens";

interface Point {
  t: number;
  value: number;
}

/** Rolling client-side history of a single numeric stat, sampled every time the
 * websocket delivers a new MigrationRecord -- the backend itself doesn't persist a
 * time series, only the latest snapshot, so the chart's window is whatever this
 * component has observed since it mounted. */
export default function ThroughputChart({ value, label }: { value: number; label: string }) {
  const [history, setHistory] = useState<Point[]>([]);
  const lastRef = useRef<number>(0);

  useEffect(() => {
    const now = Date.now();
    if (now - lastRef.current < 900) return;
    lastRef.current = now;
    setHistory((prev) => [...prev.slice(-59), { t: now, value }]);
  }, [value]);

  const latest = history[history.length - 1];

  return (
    <div className="cb-card" style={{ padding: 16 }}>
      <div style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 700, textTransform: "uppercase", marginBottom: 8 }}>
        {label}
      </div>
      <ResponsiveContainer width="100%" height={140}>
        <AreaChart data={history} margin={{ top: 28, right: 12, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="throughputFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colors.cbTeal} stopOpacity={0.5} />
              <stop offset="100%" stopColor={colors.cbTeal} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="t" hide />
          <YAxis
            domain={[0, "auto"]}
            width={40}
            axisLine={false}
            tickLine={false}
            tick={{ fill: colors.textMuted, fontSize: 10.5 }}
            tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0))}
          />
          <Tooltip
            contentStyle={{ background: colors.bg2, border: `1px solid ${colors.borderSubtle}`, borderRadius: 8, fontSize: 12 }}
            labelFormatter={() => ""}
            formatter={(v: number) => [v.toFixed(1), label]}
            cursor={{ stroke: colors.borderStrong, strokeWidth: 1 }}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke={colors.cbTeal}
            strokeWidth={2}
            fill="url(#throughputFill)"
            isAnimationActive={false}
            activeDot={{ r: 4, fill: colors.cbTeal, stroke: colors.bg1, strokeWidth: 2 }}
          />
          {/* Pins the live value at the most recent point so the current rate is
              always visible without hovering; hovering elsewhere still shows that
              point's own value via the Tooltip above. */}
          {latest && (
            <>
              <ReferenceLine x={latest.t} stroke={colors.borderStrong} strokeWidth={1} />
              <ReferenceDot
                x={latest.t}
                y={latest.value}
                r={4}
                fill={colors.cbTeal}
                stroke={colors.bg1}
                strokeWidth={2}
                isFront
                label={<LiveValueLabel text={`${label} : ${latest.value.toFixed(1)}`} />}
              />
            </>
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function LiveValueLabel(props: {
  viewBox?: { x: number; y: number; width: number; height: number };
  text: string;
}) {
  const { viewBox, text } = props;
  if (!viewBox) return null;
  const cx = viewBox.x + viewBox.width / 2;
  const cy = viewBox.y + viewBox.height / 2;
  const boxWidth = Math.max(64, text.length * 6.2 + 20);
  const boxHeight = 22;
  const x = cx - boxWidth - 8;
  const y = Math.max(cy - boxHeight - 10, 2);

  return (
    <g>
      <rect x={x} y={y} width={boxWidth} height={boxHeight} rx={6} fill={colors.bg2} stroke={colors.borderSubtle} />
      <text x={x + boxWidth / 2} y={y + boxHeight / 2 + 4} textAnchor="middle" fontSize={11} fontWeight={700} fill={colors.cbTeal}>
        {text}
      </text>
    </g>
  );
}
