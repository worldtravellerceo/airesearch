"use client";

import { useCallback, useEffect, useState } from "react";

import { CompareChart } from "@/components/CompareChart";
import { loadSeries, type CompareSeries } from "@/lib/compare";
import type { IndexEntry } from "@/lib/types";

const MAX_SERIES = 4;

/** Pick up to four projects and put their curves on one axis.
 *
 *  Runs entirely in the browser: each project's history is already a static
 *  file, so there is nothing to ask a server for. The selection lives in the
 *  URL so a comparison can be linked to.
 */
export function CompareBoard({ index }: { index: IndexEntry[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [series, setSeries] = useState<CompareSeries[]>([]);
  const [missing, setMissing] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");

  // Seed from ?repos= so a comparison survives being shared or bookmarked.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const initial = (params.get("repos") ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean)
      .slice(0, MAX_SERIES);
    if (initial.length) setSelected(initial);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!selected.length) {
      setSeries([]);
      setMissing([]);
      return;
    }
    setLoading(true);
    Promise.all(selected.map(loadSeries)).then((loaded) => {
      if (cancelled) return;
      setSeries(loaded.filter((item): item is CompareSeries => item !== null));
      setMissing(selected.filter((_, i) => loaded[i] === null));
      setLoading(false);
    });

    const url = new URL(window.location.href);
    url.searchParams.set("repos", selected.join(","));
    window.history.replaceState(null, "", url);
    return () => {
      cancelled = true;
    };
  }, [selected]);

  const add = useCallback((fullName: string) => {
    setSelected((current) =>
      current.includes(fullName) || current.length >= MAX_SERIES
        ? current
        : [...current, fullName],
    );
    setQuery("");
  }, []);

  const suggestions = query.trim().length >= 2
    ? index
        .filter((row) => row.full_name.toLowerCase().includes(query.trim().toLowerCase()))
        .filter((row) => !selected.includes(row.full_name))
        .slice(0, 8)
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">Karşılaştır</h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          En fazla dört projeyi aynı eksende görün. Varsayılan eksen takvim değil,
          her projenin kendi yaşı.
        </p>
      </div>

      <div className="space-y-2">
        <div className="relative">
          <label htmlFor="compare-search" className="sr-only">
            Proje ekle
          </label>
          <input
            id="compare-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={
              selected.length >= MAX_SERIES
                ? "Dört proje seçildi — birini çıkarın"
                : "Proje ara ve ekle"
            }
            disabled={selected.length >= MAX_SERIES}
            autoComplete="off"
            className="border-border bg-surface-1 text-ink placeholder:text-ink-muted w-full rounded-md border px-3 py-2 text-sm disabled:opacity-50 md:w-96"
          />
          {suggestions.length ? (
            <ul className="border-border bg-surface-1 absolute z-20 mt-1 w-full overflow-hidden rounded-md border shadow-lg md:w-96">
              {suggestions.map((row) => (
                <li key={row.full_name}>
                  <button
                    type="button"
                    onClick={() => add(row.full_name)}
                    className="hover:bg-surface-2 block w-full px-3 py-2 text-left text-sm"
                  >
                    {row.full_name}
                    <span className="text-ink-muted ml-2 text-xs">
                      {row.stars.toLocaleString("tr-TR")} ★
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>

        {selected.length ? (
          <div className="flex flex-wrap gap-1.5">
            {selected.map((fullName) => (
              <button
                key={fullName}
                type="button"
                onClick={() =>
                  setSelected((current) => current.filter((item) => item !== fullName))
                }
                className="border-border text-ink-secondary hover:bg-surface-2 rounded-full border px-3 py-1 text-xs"
                aria-label={`${fullName} karşılaştırmadan çıkar`}
              >
                {fullName} ✕
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {missing.length ? (
        <p className="text-ink-muted text-xs">
          Detay sayfası olmayanlar atlandı: {missing.join(", ")}
        </p>
      ) : null}

      {series.length ? (
        <section className="bg-surface-1 border-border rounded-lg border p-4">
          <div className={loading ? "opacity-50 transition" : "transition"}>
            <CompareChart series={series} />
          </div>
        </section>
      ) : (
        <p className="text-ink-muted border-border rounded-lg border border-dashed p-8 text-center text-sm">
          {loading ? "Yükleniyor…" : "Karşılaştırmak için yukarıdan proje ekleyin."}
        </p>
      )}
    </div>
  );
}
