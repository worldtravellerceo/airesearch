"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { BoardTable } from "@/components/BoardTable";
import { dataUrl } from "@/lib/paths";
import {
  ALL_CATEGORIES,
  BOARDS,
  BOARD_COPY,
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
export function BoardView({
  board,
  initialEntries,
  categories,
  boardCounts,
}: {
  board: Board;
  initialEntries: BoardEntry[];
  categories: CategoryRow[];
  /** Entry count per board, so an empty one can point at a populated one. */
  boardCounts: Record<string, number>;
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
                {count ? (
                  <span className="tabular text-ink-muted text-xs">{count}</span>
                ) : null}
              </Link>
            );
          })}
        </nav>
        <p className="text-ink-secondary mt-3 max-w-3xl text-sm">
          {BOARD_COPY[board].blurb}
        </p>
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
              <span className="tabular opacity-60">{count(row.repos)}</span>
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
          <BoardTable
            board={board}
            entries={entries}
            populated={populatedElsewhere}
          />
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
