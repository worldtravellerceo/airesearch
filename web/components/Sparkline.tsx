/** A 90-day star-velocity sparkline for a table row.
 *
 *  Plain SVG rather than a chart library: one of these renders per row, and a
 *  full charting runtime per row is a lot of JavaScript for a shape with no
 *  axes. It carries no legend by design — the column header names it, and the
 *  numeric columns beside it carry the actual values. */
export function Sparkline({
  values,
  width = 96,
  height = 26,
  label,
}: {
  values: number[] | null | undefined;
  width?: number;
  height?: number;
  label?: string;
}) {
  if (!values || values.length < 2) {
    return (
      <span className="text-ink-muted text-xs" aria-label="yeterli geçmiş yok">
        —
      </span>
    );
  }

  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const y = (value: number) => height - 1 - (value / max) * (height - 2);

  const line = values.map((value, i) => `${i * step},${y(value)}`).join(" ");
  const area = `0,${height} ${line} ${width},${height}`;
  const last = values[values.length - 1];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label ?? `Son ${values.length} günün yıldız hızı`}
      className="overflow-visible"
    >
      <polygon points={area} fill="var(--series-1)" opacity={0.12} />
      <polyline
        points={line}
        fill="none"
        stroke="var(--series-1)"
        strokeWidth={1.75}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {/* The trailing point anchors the eye to "now", which is the value the
          surrounding columns are about. */}
      <circle
        cx={width}
        cy={y(last)}
        r={2.25}
        fill="var(--series-1)"
        stroke="var(--surface-1)"
        strokeWidth={1.5}
      />
    </svg>
  );
}
