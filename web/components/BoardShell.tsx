import { BoardView } from "@/components/BoardView";
import { SearchBox } from "@/components/SearchBox";
import { StatTile } from "@/components/StatTile";
import { getBoard, getCategories, getOverview, getSearchIndex } from "@/lib/data";
import { compact, count, shortDate } from "@/lib/format";
import { ALL_CATEGORIES, type Board } from "@/lib/types";

/** The whole board page, assembled at build time.
 *
 *  Everything the first paint needs is inlined; only the category filter and
 *  search reach for another file, and both are static assets.
 */
export async function BoardShell({ board }: { board: Board }) {
  const [overview, categories, file, index] = await Promise.all([
    getOverview(),
    getCategories(),
    getBoard(board, ALL_CATEGORIES),
    getSearchIndex(),
  ]);

  const entries = file?.entries ?? [];

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-ink text-2xl font-semibold tracking-tight">
            GitHub yapay zeka ekosistemi
          </h1>
          <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
            Aynı repolar, dört farklı soruya göre sıralanıyor. Listelerin birbirinden
            farklı çıkması bir tutarsızlık değil — bütün mesele o.
          </p>
        </div>
        {index.length ? <SearchBox total={index.length} /> : null}
      </section>

      {overview ? (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatTile
            label="İzlenen AI projesi"
            value={count(overview.counts.ai_repos)}
            hint={`${count(overview.counts.tracked)} repo taranıyor`}
          />
          <StatTile
            label="Tam geçmişi çıkarılmış"
            value={count(overview.counts.backfilled)}
            hint="Fresh Power sadece bunları sıralar"
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

      {entries.length === 0 ? (
        <section className="border-border rounded-lg border border-dashed p-10 text-center">
          <p className="text-ink font-medium">Henüz veri yok.</p>
          <p className="text-ink-muted mt-2 text-sm">
            İlk toplama turu çalıştığında board&apos;lar burada görünecek.
          </p>
        </section>
      ) : (
        <BoardView board={board} initialEntries={entries} categories={categories} />
      )}
    </div>
  );
}
