/** The backend sends naive UTC timestamps (no offset); treat them as UTC. */
export function parseUtc(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function formatUtc(iso?: string | null): string {
  return iso ? parseUtc(iso).toLocaleString() : "--";
}

export function ago(iso?: string | null): string {
  if (!iso) return "--";
  const s = Math.max(0, (Date.now() - parseUtc(iso).getTime()) / 1000);
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}
