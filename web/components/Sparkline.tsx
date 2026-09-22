/** A 90-day star-velocity sparkline for a table row.
 *
 *  Plain SVG rather than a chart library: one of these renders per row, and a
 *  full charting runtime per row is a lot of JavaScript for a shape with no
 *  axes. It carries no legend by design — the column header names it, and the
 *  numeric columns beside it carry the actual values.
 *
 *  The grid is fixed at `span` days and the series is right-aligned inside it.
 *  Before this, every row said "90 gün" and stretched whatever it had across
 *  the same 96 pixels, so a repository with two recorded days and one with a
 *  full window drew the same width — two different facts as one shape. A repo
 *  we have watched for a week now occupies a week of the axis, and the empty
 *  part is visibly empty. */

/** Below this there is no shape to read, only a line between two points. */
const MIN_POINTS = 3;

export function Sparkline({
  values,
  from,
  until,
  span = 90,
  width = 96,
  height = 26,
  label,
}: {
  values: Array<number | null> | null | undefined;
  /** ISO date of the first value. Missing means the series is undated and is
   *  drawn flush right, which is what it did before the grid existed. */
  from?: string | null;
  /** ISO date the window ends on — the export date. */
  until?: string | null;
  span?: number;
  width?: number;
  height?: number;
  label?: string;
}) {
  const points = values?.filter((v): v is number => v !== null && v !== undefined) ?? [];
  if (!values || points.length < MIN_POINTS) {
    return (
      <span
        className="text-ink-muted text-xs"
        title={
          points.length
            ? `${points.length} günlük kayıt — çizmek için yeterli değil`
            : "yıldız geçmişi yok"
        }
      >
        —
      </span>
    );
  }

  // Where the series sits on the fixed grid. `offset` is the day index of the
  // first value; a series older than the window is clipped to it.
  const dayOffset = from && until ? daysBetween(from, until) : values.length - 1;
  const offset = Math.max(0, span - 1 - Math.min(dayOffset, span - 1));
  const drawn = values.slice(Math.max(0, values.length - span));

  const step = width / (span - 1);
  const max = Math.max(...points, 1);
  const y = (value: number) => height - 1 - (value / max) * (height - 2);
  const x = (i: number) => (offset + i) * step;

  // A gap inside the series is a day with no row, not a day with no stars, so
  // the line breaks rather than dipping to zero through it.
  const segments: string[] = [];
  let current: string[] = [];
  drawn.forEach((value, i) => {
    if (value === null || value === undefined) {
      if (current.length > 1) segments.push(current.join(" "));
      current = [];
      return;
    }
    current.push(`${x(i)},${y(value)}`);
  });
  if (current.length > 1) segments.push(current.join(" "));

  let lastIndex = -1;
  for (let i = 0; i < drawn.length; i += 1) {
    const value = drawn[i];
    if (value !== null && value !== undefined) lastIndex = i;
  }
  const last = lastIndex >= 0 ? (drawn[lastIndex] ?? 0) : 0;
  // Only the final unbroken run is filled: shading across a gap would draw an
  // area over days nobody measured.
  const tail = segments.length ? segments[segments.length - 1] : "";
  const tailStart = tail ? tail.split(" ")[0].split(",")[0] : "0";
  const area = tail ? `${tailStart},${height} ${tail} ${x(lastIndex)},${height}` : "";

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={label ?? `Son ${drawn.length} günün yıldız hızı`}
      className="overflow-visible"
    >
      {area ? <polygon points={area} fill="var(--series-1)" opacity={0.12} /> : null}
      {segments.map((segment, i) => (
        <polyline
          key={i}
          points={segment}
          fill="none"
          stroke="var(--series-1)"
          strokeWidth={1.75}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      ))}
      {/* The trailing point anchors the eye to "now", which is the value the
          surrounding columns are about. */}
      <circle
        cx={x(lastIndex)}
        cy={y(last)}
        r={2.25}
        fill="var(--series-1)"
        stroke="var(--surface-1)"
        strokeWidth={1.5}
      />
    </svg>
  );
}

function daysBetween(from: string, until: string): number {
  const a = Date.parse(from);
  const b = Date.parse(until);
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return Math.round((b - a) / 86_400_000);
}
