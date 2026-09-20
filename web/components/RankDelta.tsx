/** Movement since the previous snapshot.
 *
 *  "New" and "unchanged" are different facts and are shown differently — a
 *  repo that just entered a board has no previous rank, which is not the same
 *  as one that held its place. */
export function RankDelta({ delta }: { delta: number | null | undefined }) {
  if (delta === null || delta === undefined) {
    return (
      <span className="text-ink-muted text-xs" title="Bu board'a yeni girdi">
        yeni
      </span>
    );
  }
  if (delta === 0) {
    return (
      <span className="text-ink-muted text-xs" title="Sıra değişmedi">
        –
      </span>
    );
  }
  const up = delta > 0;
  return (
    <span
      className={`tabular text-xs font-medium ${up ? "text-good" : "text-critical"}`}
      title={up ? `${delta} sıra yükseldi` : `${-delta} sıra düştü`}
    >
      {up ? "▲" : "▼"} {Math.abs(delta)}
    </span>
  );
}
