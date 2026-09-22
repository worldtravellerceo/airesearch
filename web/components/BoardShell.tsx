import { BoardView } from "@/components/BoardView";
import { SearchBox } from "@/components/SearchBox";
import { StatTile } from "@/components/StatTile";
import { getBoard, getBoardCounts, getCategoryFile, getOverview, getSearchIndex } from "@/lib/data";
import { compact, count, shortDate } from "@/lib/format";
import { ALL_CATEGORIES, type Board } from "@/lib/types";

/** The whole board page, assembled at build time.
 *
 *  Everything the first paint needs is inlined; only the category filter and
 *  search reach for another file, and both are static assets.
 */
export async function BoardShell({ board }: { board: Board }) {
  const [overview, categoryFile, file, index, boardCounts] = await Promise.all([
    getOverview(),
    getCategoryFile(),
    getBoard(board, ALL_CATEGORIES),
    getSearchIndex(),
    getBoardCounts(),
  ]);
  const categories = categoryFile.categories;

  const entries = file?.entries ?? [];

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-ink text-2xl font-semibold tracking-tight">
            GitHub yapay zeka ekosistemi
          </h1>
          <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
            Aynı repolar, dört farklı soruya göre sıralanıyor. Listelerin birbirinden farklı çıkması
            bir tutarsızlık değil — bütün mesele o.
          </p>
        </div>
        {index.length ? <SearchBox total={index.length} /> : null}
      </section>

      {overview ? (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {/* Two numbers, not one. Of the repositories classified as AI, under
              a third have any star history at all — the rest were read off
              their metadata and have never been watched, and their velocities
              are zero because the column defaults to zero. A single tile
              claimed all of it had been observed. */}
          <StatTile
            label="Sınıflandırılan AI projesi"
            value={count(overview.counts.ai_repos)}
            hint={`${count(overview.counts.tracked)} repo tarandı${
              overview.counts.ai_unsettled
                ? ` · ${count(overview.counts.ai_unsettled)} karara bağlanmadı`
                : ""
            }`}
          />
          <StatTile
            label="Yıldız geçmişi ölçülen"
            value={count(overview.counts.ai_measured)}
            hint={
              overview.counts.pools?.fresh !== undefined
                ? `Fresh Power ${count(overview.counts.pools.fresh)} projeyi sıralıyor`
                : "Hız ve ivme yalnızca bunlar için gerçek"
            }
          />
          <StatTile
            label="Günlük veri noktası"
            value={compact(overview.counts.day_rows)}
            hint="repo × gün"
          />
          <StatTile
            label="Son güncelleme"
            value={shortDate(overview.as_of)}
            hint={
              overview.last_run
                ? `${overview.last_run.command} · ${count(overview.last_run.api_calls)} API isteği`
                : undefined
            }
          />
        </section>
      ) : null}

      {/* Always rendered, even when this board is empty: the tabs live inside
          it, and hiding them strands a visitor on the one board that has
          nothing to show. */}
      <BoardView
        board={board}
        initialEntries={entries}
        categories={categories}
        boardCounts={boardCounts}
        categoryPools={categoryFile.pools[board] ?? {}}
        asOf={file?.as_of ?? overview?.as_of ?? null}
      />
    </div>
  );
}
