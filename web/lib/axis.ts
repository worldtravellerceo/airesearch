/** Date-axis tick formatting that adapts to the span.
 *
 *  A fixed format is wrong at one end or the other: on a two-week series
 *  "month year" prints the same label on every tick, and on a five-year series
 *  "day month" prints far too many distinct ones. */
export function dateTickFormatter(points: Array<{ date: string }>) {
  const spanDays = spanInDays(points);
  if (spanDays <= 45) {
    return (value: string) =>
      new Date(value).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
  }
  if (spanDays <= 730) {
    return (value: string) =>
      new Date(value).toLocaleDateString("tr-TR", { month: "short", year: "2-digit" });
  }
  return (value: string) => String(new Date(value).getFullYear());
}

export function spanInDays(points: Array<{ date: string }>): number {
  if (points.length < 2) return 0;
  const first = new Date(points[0].date).getTime();
  const last = new Date(points[points.length - 1].date).getTime();
  return Math.round((last - first) / 86_400_000);
}
