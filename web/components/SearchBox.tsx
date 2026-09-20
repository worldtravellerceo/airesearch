"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { categoryLabel, compact } from "@/lib/format";
import { repoSlug } from "@/lib/paths";
import type { IndexEntry } from "@/lib/types";

/** Search over the whole tracked universe, in the browser.
 *
 *  The index ships with the page: a few thousand rows is small enough to filter
 *  locally and means search works without a server behind it. */
export function SearchBox({ index }: { index: IndexEntry[] }) {
  const [query, setQuery] = useState("");

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (needle.length < 2) return [];
    return index
      .filter(
        (row) =>
          row.full_name.toLowerCase().includes(needle) ||
          (row.description ?? "").toLowerCase().includes(needle) ||
          (row.one_liner ?? "").toLowerCase().includes(needle),
      )
      .slice(0, 12);
  }, [index, query]);

  return (
    <div className="relative">
      <label htmlFor="repo-search" className="sr-only">
        Proje ara
      </label>
      <input
        id="repo-search"
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder={`Ara (${compact(index.length)} proje)`}
        autoComplete="off"
        className="border-border bg-surface-1 text-ink placeholder:text-ink-muted w-full rounded-md border px-3 py-1.5 text-sm md:w-72"
      />
      {results.length ? (
        <ul className="border-border bg-surface-1 absolute z-20 mt-1 w-full overflow-hidden rounded-md border shadow-lg md:w-96">
          {results.map((row) => {
            const { owner, name } = repoSlug(row.full_name);
            return (
              <li key={row.full_name}>
                <Link
                  href={`/repos/${owner}/${name}/`}
                  onClick={() => setQuery("")}
                  className="hover:bg-surface-2 block px-3 py-2"
                >
                  <span className="text-ink text-sm">{row.full_name}</span>
                  <span className="text-ink-muted ml-2 text-xs">
                    {compact(row.stars)} ★ · {categoryLabel(row.category)}
                  </span>
                  <span className="text-ink-muted line-clamp-1 text-xs">
                    {row.one_liner ?? row.description ?? ""}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
