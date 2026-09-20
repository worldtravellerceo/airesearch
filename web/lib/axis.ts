/** Date-axis ticks and labels that adapt to the span.
 *
 *  Two separate problems, both of which show up as unreadable axes:
 *
 *  - A fixed label format is wrong at one end or the other. On a two-week series
 *    "month year" prints the same text on every tick; on a five-year series
 *    "day month" prints far too many distinct ones.
 *  - Letting the chart place ticks freely on a long series repeats the same
 *    year several times over, because the labels are coarser than the spacing.
 *    The fix is to choose the tick positions from the data — one per year, or
 *    one per month — rather than to relabel whatever positions it picked.
 */

export type DatedPoint = { date: string };

export function spanInDays(points: DatedPoint[]): number {
  if (points.length < 2) return 0;
  const first = new Date(points[0].date).getTime();
  const last = new Date(points[points.length - 1].date).getTime();
  return Math.round((last - first) / 86_400_000);
}

export function dateTickFormatter(points: DatedPoint[]) {
  const span = spanInDays(points);
  if (span <= 45) {
    return (value: string) =>
      new Date(value).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
  }
  if (span <= 730) {
    return (value: string) =>
      new Date(value).toLocaleDateString("tr-TR", { month: "short", year: "2-digit" });
  }
  return (value: string) => String(new Date(value).getFullYear());
}

/** Explicit tick positions, or `undefined` to let the chart place them.
 *
 *  Returns dates that exist in the series — a tick on a date the series does not
 *  contain renders nowhere on a category axis.
 */
export function dateTicks(points: DatedPoint[], maxTicks = 12): string[] | undefined {
  const span = spanInDays(points);
  if (span <= 45 || points.length < 3) return undefined;

  const unit: "year" | "month" = span > 730 ? "year" : "month";
  const seen = new Set<string>();
  const ticks: string[] = [];

  for (const point of points) {
    const date = new Date(point.date);
    const key =
      unit === "year"
        ? String(date.getFullYear())
        : `${date.getFullYear()}-${date.getMonth()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    ticks.push(point.date);
  }

  // Thin evenly rather than truncating, so the axis still spans the whole range.
  if (ticks.length > maxTicks) {
    const step = Math.ceil(ticks.length / maxTicks);
    return ticks.filter((_, index) => index % step === 0);
  }
  return ticks;
}
