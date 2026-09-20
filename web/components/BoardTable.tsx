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
}: {
  board: Board;
  entries: BoardEntry[];
  /** Boards that do have data, so an empty one can point at them. */
  populated?: Board[];
}) {
  if (!entries.length) {
    return (
      <div className="border-border rounded-lg border border-dashed p-10 text-center">
        <p className="text-ink font-medium">Bu board henüz boş</p>
        <p className="text-ink-muted mx-auto mt-2 max-w-xl text-sm">
          {WAITING_ON[board]}
        </p>
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
        <caption className="sr-only">
          {board} board&apos;u, sıra ve büyüme metrikleriyle
        </caption>
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
            <th scope="col" className="py-2.5 pr-4 font-medium">
              90 gün
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
                  label={`${entry.full_name} son 90 günün yıldız hızı`}
                />
              </td>
              <td className="tabular py-2.5 pr-4 text-right">{rate(entry.velocity_14d)}</td>
              <td className="tabular py-2.5 pr-4 text-right">
                {multiple(entry.acceleration)}
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
      return compact(entry.fresh_power);
    case "momentum":
      return rate(entry.velocity_14d);
    case "breakout":
      return multiple(entry.acceleration);
    case "popular":
      return count(entry.stars);
  }
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
