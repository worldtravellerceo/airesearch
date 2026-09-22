import Link from "next/link";

import { repoSlug } from "@/lib/paths";
import type { Board, BoardEntry } from "@/lib/types";
import { categoryLabel, compact, count, days, multiple, rate } from "@/lib/format";
import { RankDelta } from "@/components/RankDelta";
import { Sparkline } from "@/components/Sparkline";

/** The leaderboard itself.
 *
 *  The score column changes with the board, because the number a board ranks by
 *  is the one thing a reader must be able to see — a Fresh Power list that only
 *  shows raw stars is unfalsifiable. */
const WAITING_ON: Record<Board, string> = {
  fresh:
    "Fresh Power bir projenin tüm ömrünü hesaba katıyor, o yüzden sadece geçmişi tamamen çıkarılmış repoları sıralıyor. Geçmiş çıkarma turu sürüyor.",
  momentum:
    "Momentum son 14 günün yıldız hızını ölçüyor; bunun için günlük geçmiş verisi gerekiyor. Toplama turu sürüyor.",
  breakout:
    "Breakout bir projenin kendi 90 günlük temposuyla karşılaştırma yapıyor; bunun için geçmiş verisi gerekiyor.",
  popular: "Henüz hiç proje toplanmadı.",
};

export function BoardTable({
  board,
  entries,
  populated = [],
  asOf = null,
}: {
  board: Board;
  entries: BoardEntry[];
  /** Boards that do have data, so an empty one can point at them. */
  populated?: Board[];
  /** The export date, so a sparkline can be placed on the window it belongs to
   *  rather than stretched to fill the column. */
  asOf?: string | null;
}) {
  if (!entries.length) {
    return (
      <div className="border-border rounded-lg border border-dashed p-10 text-center">
        <p className="text-ink font-medium">Bu board henüz boş</p>
        <p className="text-ink-muted mx-auto mt-2 max-w-xl text-sm">{WAITING_ON[board]}</p>
        {populated.length ? (
          <p className="text-ink-secondary mt-4 text-sm">
            Şimdilik bakabileceklerin:{" "}
            {populated.map((value, index) => (
              <span key={value}>
                {index > 0 ? ", " : ""}
                <Link
                  href={value === "fresh" ? "/" : `/board/${value}/`}
                  className="text-accent hover:underline"
                >
                  {BOARD_LABELS[value]}
                </Link>
              </span>
            ))}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="border-border bg-surface-1 overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[880px] text-sm">
        <caption className="sr-only">{board} board&apos;u, sıra ve büyüme metrikleriyle</caption>
        <thead>
          <tr className="text-ink-muted border-border border-b text-left text-xs">
            <th scope="col" className="py-2.5 pr-2 pl-4 font-medium">
              #
            </th>
            <th scope="col" className="py-2.5 pr-3 font-medium">
              Δ
            </th>
            <th scope="col" className="py-2.5 pr-4 font-medium">
              Proje
            </th>
            <th scope="col" className="py-2.5 pr-4 font-medium">
              Kategori
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              Yıldız
            </th>
            {/* "90 gün" alone read as a cumulative curve, which is what the
                project page's lifetime chart shows. This one is a rate: stars
                gained per day. A falling line here means the project is
                growing more slowly, not that it lost stars — and the two
                shapes disagreeing is normal. */}
            <th
              scope="col"
              className="py-2.5 pr-4 font-medium"
              title="Günde kazanılan yıldız — toplam değil. Çizginin düşmesi yavaşlama demek, yıldız kaybı değil."
            >
              90g günlük hız
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              14g hız
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              İvme
            </th>
            <th scope="col" className="py-2.5 pr-4 text-right font-medium">
              {scoreHeading(board)}
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr
              key={entry.repo_id}
              className="border-border/60 hover:bg-surface-2/60 border-b transition last:border-0"
            >
              <td className="tabular text-ink-secondary py-2.5 pr-2 pl-4">{entry.rank}</td>
              <td className="py-2.5 pr-3">
                <RankDelta delta={entry.rank_delta} />
              </td>
              <td className="py-2.5 pr-4">
                <Link
                  href={`/repos/${repoSlug(entry.full_name).owner}/${repoSlug(entry.full_name).name}/`}
                  className="text-ink hover:text-accent font-medium"
                >
                  {entry.full_name}
                </Link>
                <GitHubLink fullName={entry.full_name} />
                {entry.breakout ? (
                  <span
                    className="ml-1.5"
                    title="Breakout: kendi temposunun en az 3 katında"
                    aria-label="breakout"
                  >
                    🔥
                  </span>
                ) : null}
                {entry.archived ? (
                  <span className="text-ink-muted ml-1.5 text-xs">(arşiv)</span>
                ) : null}
                <div className="text-ink-muted mt-0.5 line-clamp-1 max-w-md text-xs">
                  {entry.one_liner ?? entry.description ?? ""}
                </div>
              </td>
              <td className="text-ink-secondary py-2.5 pr-4 text-xs">
                {categoryLabel(entry.category)}
              </td>
              <td className="tabular py-2.5 pr-4 text-right">{compact(entry.stars)}</td>
              <td className="py-2.5 pr-4">
                <Sparkline
                  values={entry.sparkline}
                  from={entry.sparkline_from}
                  until={asOf}
                  label={`${entry.full_name} son 90 günün günlük yıldız hızı`}
                />
              </td>
              <td className="tabular py-2.5 pr-4 text-right">{rate(entry.velocity_14d)}</td>
              <td className="tabular py-2.5 pr-4 text-right">
                <Acceleration entry={entry} />
              </td>
              <td className="tabular text-ink py-2.5 pr-4 text-right font-medium">
                {scoreValue(board, entry)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A link out to the repository itself.
 *
 *  The project name already goes to our own detail page, so this cannot be the
 *  same click. It is a separate target with its own label, sized to stay
 *  tappable on a phone without stealing width from the name beside it. */
export function GitHubLink({ fullName }: { fullName: string }) {
  return (
    <a
      href={`https://github.com/${fullName}`}
      target="_blank"
      rel="noopener noreferrer"
      title={`${fullName} — GitHub'da aç`}
      aria-label={`${fullName} projesini GitHub'da aç`}
      className="text-ink-muted hover:text-accent hover:border-border focus-visible:ring-accent ml-1.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border border-transparent align-middle transition focus-visible:ring-2 focus-visible:outline-none"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true" className="h-3.5 w-3.5" fill="currentColor">
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
      </svg>
    </a>
  );
}

const BOARD_LABELS: Record<Board, string> = {
  fresh: "Fresh Power",
  momentum: "Momentum",
  breakout: "Breakout",
  popular: "Popüler",
};

function scoreHeading(board: Board): string {
  return {
    fresh: "Fresh Power",
    momentum: "Momentum",
    breakout: "Breakout",
    popular: "Toplam",
  }[board];
}

function scoreValue(board: Board, entry: BoardEntry): string {
  switch (board) {
    case "fresh":
      // A repo whose history does not reach its creation date has a Fresh
      // Power that is a floor, not a total — the days we never pulled can only
      // add to it. 790 detail pages printed the floor as a flat number.
      return entry.history_backfilled_through
        ? compact(entry.fresh_power)
        : `≥ ${compact(entry.fresh_power)}`;
    case "momentum":
      return rate(entry.velocity_14d);
    case "breakout":
      return multiple(entry.acceleration);
    case "popular":
      return count(entry.stars);
  }
}

/** Acceleration, with the two values the metric code invents marked as such.
 *
 *  `_acceleration` reports 1.0 for a repository too young to have a baseline
 *  and a cap for one that was dormant and suddenly woke. Both reached the
 *  Breakout board — the board whose whole subject is acceleration — printed
 *  exactly like a reading taken off ninety days of history. */
export function Acceleration({
  entry,
}: {
  entry: Pick<BoardEntry, "acceleration" | "acceleration_basis">;
}) {
  const basis = entry.acceleration_basis;
  if (basis === "too_young") {
    return (
      <span className="text-ink-muted" title="Henüz kendi temposu ölçülemedi — çok yeni">
        —
      </span>
    );
  }
  if (basis === "no_baseline") {
    return (
      <span
        className="text-ink-secondary"
        title="Uzun süre hareketsizdi: oran sıfıra bölünmesin diye tavanlandı"
      >
        ↑ {multiple(entry.acceleration)}
      </span>
    );
  }
  return <>{multiple(entry.acceleration)}</>;
}

/** Milestones are the most direct answer to "five years or two weeks?", so they
 *  get their own strip on the detail page. */
export function MilestoneStrip({
  entry,
}: {
  entry: Pick<BoardEntry, "days_to_1k" | "days_to_10k" | "days_to_50k">;
}) {
  const milestones: Array<[string, number | null]> = [
    ["1.000 yıldıza", entry.days_to_1k],
    ["10.000 yıldıza", entry.days_to_10k],
    ["50.000 yıldıza", entry.days_to_50k],
  ];
  const reached = milestones.filter(([, value]) => value !== null);
  if (!reached.length) {
    return (
      <p className="text-ink-muted text-sm">
        Kilometre taşları için tam geçmiş gerekiyor — bu repo henüz backfill edilmedi.
      </p>
    );
  }
  return (
    <dl className="flex flex-wrap gap-6">
      {reached.map(([label, value]) => (
        <div key={label}>
          <dt className="text-ink-muted text-xs">{label}</dt>
          <dd className="tabular text-ink text-lg font-semibold">{days(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
