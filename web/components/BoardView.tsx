"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { BoardTable } from "@/components/BoardTable";
import { dataUrl } from "@/lib/paths";
import {
  ALL_CATEGORIES,
  BOARDS,
  BOARD_COPY,
  type BoardMovement,
  type Board,
  type BoardEntry,
  type CategoryRow,
} from "@/lib/types";
import { categoryLabel, count } from "@/lib/format";

/** Board tabs plus the category filter.
 *
 *  The board the visitor arrived on is inlined by the build, so the first paint
 *  needs no JavaScript and no round trip. Switching category fetches one small
 *  static file — pre-rendering all sixty board/category combinations would bloat
 *  the build for pages most people never open.
 */
/** What changed on this board since the last run.
 *
 *  Stillness is reported rather than hidden. Fresh Power integrates a lifetime
 *  at a 180-day half-life and Popular is a running total, so neither is meant
 *  to jump: measured on 2026-10-06, every single rank change on both was three
 *  places or fewer, against 103 rows moving five or more on Momentum. A page
 *  of "–" with no explanation invites the reader to conclude the site is
 *  broken, which is the opposite of what those two boards are telling them.
 */
function Movement({ movement }: { movement?: BoardMovement | null }) {
  if (!movement || !movement.compared) return null;

  const { entered, moved_far: far, biggest_move: biggest, top_climber: climber } = movement;
  // Stillness is "nothing moved far", not "nothing happened at all". Fresh
  // Power took one new entry on 2026-10-06 and still had no row move more than
  // three places; keying the explanation off `entered` as well would have
  // swallowed it and left the board looking inexplicably frozen again.
  const quiet = far === 0;

  return (
    <p className="text-ink-secondary mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className="text-ink-muted">
        {movement.since ? `${movement.since} karşılaştırması:` : "önceki tura göre:"}
      </span>
      {entered ? <span className="text-ink font-medium">{entered} yeni giriş</span> : null}
      {quiet ? (
        <span>
          sıralama yerinde — en büyük oynama {biggest} basamak. Bu board uzun vadeyi ölçüyor,
          günlük değişim beklenmez.
        </span>
      ) : (
        <>
          {far ? <span>{far} repo 5+ basamak oynadı</span> : null}
          {climber ? (
            <span>
              en çok yükselen{" "}
              <Link href={`/repos/${climber.full_name}/`} className="text-ink hover:underline">
                {climber.full_name}
              </Link>{" "}
              <span className="text-good font-medium">▲ {climber.places}</span>
            </span>
          ) : null}
        </>
      )}
    </p>
  );
}

export function BoardView({
  board,
  initialEntries,
  categories,
  boardCounts,
  categoryPools = {},
  asOf = null,
  movement = null,
}: {
  board: Board;
  initialEntries: BoardEntry[];
  categories: CategoryRow[];
  /** Churn since the previous snapshot; null before a second run exists. */
  movement?: BoardMovement | null;
  /** Entry count per board, so an empty one can point at a populated one. */
  boardCounts: Record<string, number>;
  /** How many repositories *this* board can rank in each category. The chips
   *  used to print the category's size across the whole universe, next to a
   *  filter that returns at most fifty rows. */
  categoryPools?: Record<string, number>;
  asOf?: string | null;
}) {
  const [category, setCategory] = useState(ALL_CATEGORIES);
  const [entries, setEntries] = useState(initialEntries);
  const [state, setState] = useState<"ready" | "loading" | "error">("ready");

  // A board that has nothing yet should say where the data is, not just that
  // it is missing.
  const populatedElsewhere = BOARDS.filter(
    (value) => value !== board && (boardCounts[value] ?? 0) > 0,
  );

  // A new board arrives as a fresh navigation, so the inlined rows replace
  // whatever the previous board had left in state.
  useEffect(() => {
    setCategory(ALL_CATEGORIES);
    setEntries(initialEntries);
    setState("ready");
  }, [board, initialEntries]);

  const select = useCallback(
    async (next: string) => {
      setCategory(next);
      if (next === ALL_CATEGORIES) {
        setEntries(initialEntries);
        setState("ready");
        return;
      }
      setState("loading");
      try {
        const response = await fetch(dataUrl("boards", board, `${next}.json`));
        if (!response.ok) throw new Error(String(response.status));
        setEntries((await response.json()).entries ?? []);
        setState("ready");
      } catch {
        setEntries([]);
        setState("error");
      }
    },
    [board, initialEntries],
  );

  return (
    <>
      <section>
        <nav className="border-border flex flex-wrap gap-1 border-b" aria-label="Board seçimi">
          {BOARDS.map((value) => {
            const active = value === board;
            const count = boardCounts[value] ?? 0;
            return (
              <Link
                key={value}
                href={value === "fresh" ? "/" : `/board/${value}/`}
                aria-current={active ? "page" : undefined}
                title={count ? `${count} proje` : "Bu board henüz boş"}
                className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition ${
                  active
                    ? "border-accent text-ink font-medium"
                    : "text-ink-secondary hover:text-ink border-transparent"
                } ${count ? "" : "opacity-50"}`}
              >
                {BOARD_COPY[value].title}
                {count ? <span className="tabular text-ink-muted text-xs">{count}</span> : null}
              </Link>
            );
          })}
        </nav>
        <p className="text-ink-secondary mt-3 max-w-3xl text-sm">{BOARD_COPY[board].blurb}</p>
        <Movement movement={movement} />
      </section>

      {/* Filtering an empty board narrows nothing, and the counts on the chips
          are of the universe rather than of this board, which would read as a
          contradiction next to "bu board henüz boş". */}
      {categories.length && initialEntries.length ? (
        <section className="flex flex-wrap gap-1.5" aria-label="Kategori filtresi">
          <Chip active={category === ALL_CATEGORIES} onSelect={() => select(ALL_CATEGORIES)}>
            Tümü
          </Chip>
          {categories.map((row) => (
            <Chip
              key={row.category}
              active={category === row.category}
              onSelect={() => select(row.category)}
            >
              {categoryLabel(row.category)}{" "}
              {categoryPools[row.category] !== undefined ? (
                <span className="tabular opacity-60">{count(categoryPools[row.category])}</span>
              ) : null}
            </Chip>
          ))}
        </section>
      ) : null}

      {state === "error" ? (
        <p className="border-border text-ink-secondary rounded-lg border border-dashed p-6 text-center text-sm">
          Bu kategori yüklenemedi. Sayfayı yenilemeyi deneyin.
        </p>
      ) : (
        <div className={state === "loading" ? "opacity-50 transition" : "transition"}>
          <BoardTable board={board} entries={entries} populated={populatedElsewhere} asOf={asOf} />
        </div>
      )}
    </>
  );
}

function Chip({
  active,
  onSelect,
  children,
}: {
  active: boolean;
  onSelect: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active
          ? "border-accent bg-surface-2 text-ink font-medium"
          : "border-border text-ink-secondary hover:bg-surface-2"
      }`}
    >
      {children}
    </button>
  );
}
