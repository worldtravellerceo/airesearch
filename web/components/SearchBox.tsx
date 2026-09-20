"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { categoryLabel, compact } from "@/lib/format";
import { dataUrl, repoSlug } from "@/lib/paths";
import type { IndexEntry } from "@/lib/types";

/** Search over the whole tracked universe, in the browser.
 *
 *  The index is fetched the first time someone actually uses the box, not
 *  inlined into the page. A static build bakes whatever a page reads into its
 *  HTML *and* its client payload, so inlining a few thousand rows would add a
 *  megabyte to every board page twice over — for a feature most visitors never
 *  touch.
 */
export function SearchBox({ total }: { total: number }) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState<IndexEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  const loading = useRef(false);

  const load = useCallback(async () => {
    if (index || loading.current) return;
    loading.current = true;
    try {
      const response = await fetch(dataUrl("index.json"));
      if (!response.ok) throw new Error(String(response.status));
      setIndex((await response.json()).repos ?? []);
    } catch {
      setFailed(true);
    } finally {
      loading.current = false;
    }
  }, [index]);

  useEffect(() => {
    if (query.trim().length >= 2) void load();
  }, [query, load]);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (needle.length < 2 || !index) return [];
    return index
      .filter(
        (row) =>
          row.full_name.toLowerCase().includes(needle) ||
          (row.description ?? "").toLowerCase().includes(needle) ||
          (row.one_liner ?? "").toLowerCase().includes(needle),
      )
      .slice(0, 12);
  }, [index, query]);

  const searching = query.trim().length >= 2;

  return (
    <div className="relative">
      <label htmlFor="repo-search" className="sr-only">
        Proje ara
      </label>
      <input
        id="repo-search"
        type="search"
        value={query}
        onFocus={() => void load()}
        onChange={(event) => setQuery(event.target.value)}
        placeholder={`Ara (${compact(total)} proje)`}
        autoComplete="off"
        className="border-border bg-surface-1 text-ink placeholder:text-ink-muted w-full rounded-md border px-3 py-1.5 text-sm md:w-72"
      />
      {searching && !results.length ? (
        <p className="text-ink-muted absolute mt-1 text-xs">
          {failed ? "Arama indeksi yüklenemedi." : !index ? "Yükleniyor…" : "Sonuç yok."}
        </p>
      ) : null}
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
