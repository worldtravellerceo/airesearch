import Link from "next/link";

import { BoardTable } from "@/components/BoardTable";
import { StatTile } from "@/components/StatTile";
import {
  api,
  ApiError,
  BOARDS,
  BOARD_COPY,
  type Board,
  type CategoryRow,
  type LeaderboardEntry,
  type Overview,
} from "@/lib/api";
import { categoryLabel, compact, count, shortDate } from "@/lib/format";

export const revalidate = 900;

type SearchParams = Promise<{ board?: string; category?: string; limit?: string }>;

export default async function HomePage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const board = (BOARDS as readonly string[]).includes(params.board ?? "")
    ? (params.board as Board)
    : "fresh";
  const category = params.category ?? "_all";
  const limit = Number(params.limit ?? 200);

  let overview: Overview | null = null;
  let categories: CategoryRow[] = [];
  let entries: LeaderboardEntry[] = [];
  let error: string | null = null;

  try {
    const [overviewResult, categoriesResult, leaderboard] = await Promise.all([
      api.overview(),
      api.categories(),
      api.leaderboard({ board, category, limit }),
    ]);
    overview = overviewResult;
    categories = categoriesResult.categories;
    entries = leaderboard.entries;
  } catch (cause) {
    error =
      cause instanceof ApiError
        ? `API ${cause.status}: ${cause.message}`
        : "API'ye ulaşılamadı.";
  }

  if (error) {
    return (
      <div className="border-border rounded-lg border border-dashed p-8 text-center">
        <p className="text-ink font-medium">{error}</p>
        <p className="text-ink-muted mt-2 text-sm">
          <code>API_BASE</code> ayarlı mı ve API ayakta mı kontrol edin.
        </p>
      </div>
    );
  }

  const copy = BOARD_COPY[board];

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">
          GitHub yapay zeka ekosistemi
        </h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          Aynı repolar, dört farklı soruya göre sıralanıyor. Listelerin birbirinden
          farklı çıkması bir tutarsızlık değil — bütün mesele o.
        </p>
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

      <section>
        <nav className="border-border flex flex-wrap gap-1 border-b" aria-label="Board seçimi">
          {BOARDS.map((value) => {
            const active = value === board;
            return (
              <Link
                key={value}
                href={`/?board=${value}${category !== "_all" ? `&category=${category}` : ""}`}
                aria-current={active ? "page" : undefined}
                className={`-mb-px border-b-2 px-3 py-2 text-sm transition ${
                  active
                    ? "border-accent text-ink font-medium"
                    : "text-ink-secondary hover:text-ink border-transparent"
                }`}
              >
                {BOARD_COPY[value].title}
              </Link>
            );
          })}
        </nav>
        <p className="text-ink-secondary mt-3 max-w-3xl text-sm">{copy.blurb}</p>
      </section>

      {categories.length ? (
        <section className="flex flex-wrap gap-1.5" aria-label="Kategori filtresi">
          <CategoryChip board={board} value="_all" active={category === "_all"}>
            Tümü
          </CategoryChip>
          {categories.map((row) => (
            <CategoryChip
              key={row.category}
              board={board}
              value={row.category}
              active={category === row.category}
            >
              {categoryLabel(row.category)}{" "}
              <span className="tabular opacity-60">{count(row.repos)}</span>
            </CategoryChip>
          ))}
        </section>
      ) : null}

      <BoardTable board={board} entries={entries} />
    </div>
  );
}

function CategoryChip({
  board,
  value,
  active,
  children,
}: {
  board: Board;
  value: string;
  active: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={`/?board=${board}${value === "_all" ? "" : `&category=${value}`}`}
      aria-current={active ? "true" : undefined}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active
          ? "border-accent bg-surface-2 text-ink font-medium"
          : "border-border text-ink-secondary hover:bg-surface-2"
      }`}
    >
      {children}
    </Link>
  );
}
