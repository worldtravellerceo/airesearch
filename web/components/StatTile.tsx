import type { ReactNode } from "react";

/** A single headline number. Not a chart — a number this important reads
 *  faster as text than as a one-bar bar chart. */
export function StatTile({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "good" | "critical";
}) {
  const toneClass =
    tone === "good" ? "text-good" : tone === "critical" ? "text-critical" : "text-ink";
  return (
    <div className="bg-surface-1 border-border rounded-lg border p-4">
      <div className="text-ink-muted text-xs font-medium tracking-wide uppercase">
        {label}
      </div>
      <div className={`tabular mt-1 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {hint ? <div className="text-ink-muted mt-1 text-xs">{hint}</div> : null}
    </div>
  );
}
