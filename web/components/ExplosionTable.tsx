"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { AiTags } from "@/components/AiTags";
import { GitHubLink, RepoProse } from "@/components/BoardTable";
import { RankDelta } from "@/components/RankDelta";
import { compact, count, days, rate } from "@/lib/format";
import { dataUrl, repoSlug } from "@/lib/paths";
import type { ExplosionBoardFile, ExplosionEntry } from "@/lib/types";

type Filter = "all" | "ai" | "other" | "built";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "Hepsi" },
  { key: "ai", label: "AI projeleri" },
  { key: "other", label: "AI dışı" },
  { key: "built", label: "AI ile geliştirilenler" },
];

function matches(entry: ExplosionEntry, filter: Filter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "ai":
      return entry.is_ai === true;
    // Not "is_ai === false": a repository the rules could not settle is not
    // an AI project either, and leaving it out would hide it from both tabs.
    case "other":
      return entry.is_ai !== true;
    case "built":
      return entry.ai_tags.includes("agent_file") || entry.ai_tags.includes("built_statement");
  }
}

/** The explosion board's rows.
 *
 *  The first rows arrive inlined by the build; the full board — 784 rows on
 *  2026-10-09, most of a megabyte with the paragraphs — is fetched only when
 *  the reader asks for all of it or filters, so the page does not ship every
 *  row twice to a reader who looks at the top twenty.
 */
export function ExplosionTable({
  slug,
  initial,
  total,
}: {
  slug: string;
  initial: ExplosionEntry[];
  total: number;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [all, setAll] = useState<ExplosionEntry[] | null>(initial.length >= total ? initial : null);
  const [wanted, setWanted] = useState(false);
  const [failed, setFailed] = useState(false);

  const needAll = wanted || filter !== "all";
  useEffect(() => {
    if (!needAll || all) return;
    let cancelled = false;
    fetch(dataUrl("patlayanlar", `${slug}.json`))
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json() as Promise<ExplosionBoardFile>;
      })
      .then((file) => {
        if (!cancelled) setAll(file.entries);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [needAll, all, slug]);

  const source = needAll && all ? all : initial;
  const rows = useMemo(() => source.filter((e) => matches(e, filter)), [source, filter]);
  const loading = needAll && !all && !failed;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="AI ilişkisine göre süz">
        {FILTERS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilter(key)}
            aria-pressed={filter === key}
            className={`rounded-full border px-3 py-1 text-xs transition ${
              filter === key
                ? "border-accent bg-surface-2 text-ink font-medium"
                : "border-border text-ink-secondary hover:bg-surface-2"
            }`}
          >
            {label}
          </button>
        ))}
        {filter !== "all" && all ? (
          <span className="text-ink-muted self-center text-xs">{count(rows.length)} proje</span>
        ) : null}
      </div>

      {failed ? (
        <p className="text-critical text-sm">
          Listenin tamamı yüklenemedi. Sayfayı yenilemeyi deneyin.
        </p>
      ) : null}

      <div className="border-border bg-surface-1 overflow-x-auto rounded-lg border">
        <table className="w-full min-w-[880px] text-sm">
          <thead>
            <tr className="border-border text-ink-muted border-b text-left text-xs">
              <th className="py-2 pr-2 pl-4 font-medium">#</th>
              <th className="py-2 pr-3 font-medium">Δ</th>
              <th className="py-2 pr-4 font-medium">Proje</th>
              <th className="py-2 pr-4 text-right font-medium">Yaş</th>
              <th className="py-2 pr-4 text-right font-medium">Yıldız</th>
              <th
                className="py-2 pr-4 text-right font-medium"
                title="Son iki census arasındaki fark. Tek gün gürültülüdür; sıralama buna göre yapılmıyor."
              >
                Son 24 saat
              </th>
              <th
                className="py-2 pr-4 text-right font-medium"
                title="Günde kazanılan yıldız, en fazla son 7 gün üzerinden. Ölçülen gün sayısı altında yazıyor."
              >
                Şu anki hız
              </th>
              <th
                className="py-2 pr-4 text-right font-medium"
                title="Açıldığı günden bugüne ortalama: toplam yıldız / yaş."
              >
                Ömür boyu hız
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((entry) => (
              <Row key={entry.full_name} entry={entry} />
            ))}
          </tbody>
        </table>
      </div>

      {loading ? <p className="text-ink-muted text-sm">Liste yükleniyor…</p> : null}
      {!needAll && total > initial.length ? (
        <button
          type="button"
          onClick={() => setWanted(true)}
          className="border-border text-ink-secondary hover:bg-surface-2 rounded border px-3 py-1.5 text-sm"
        >
          Tümünü göster ({count(total)})
        </button>
      ) : null}
    </div>
  );
}

function Row({ entry }: { entry: ExplosionEntry }) {
  const { owner, name } = repoSlug(entry.full_name);
  const prose = Boolean(entry.description_tr || entry.usage_tr);
  return (
    <>
      <tr className="border-border/60 hover:bg-surface-2/60 border-b transition last:border-0">
        <td className="tabular text-ink-secondary py-2.5 pr-2 pl-4 align-top">{entry.rank}</td>
        <td className="py-2.5 pr-3 align-top">
          <RankDelta delta={entry.rank_delta} />
        </td>
        <td className="py-2.5 pr-4 align-top">
          {/* Our page when we wrote one, GitHub otherwise: a detail page exists
              for the AI universe only, and a link to a page that was never
              written is the bug /bugun shipped. */}
          {entry.has_page ? (
            <Link href={`/repos/${owner}/${name}/`} className="text-ink hover:text-accent font-medium">
              {entry.full_name}
            </Link>
          ) : (
            <a
              href={`https://github.com/${entry.full_name}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-ink hover:text-accent font-medium"
            >
              {entry.full_name}
            </a>
          )}
          {entry.has_page ? <GitHubLink fullName={entry.full_name} /> : null}
          {entry.language ? (
            <span className="text-ink-muted ml-1.5 text-xs">{entry.language}</span>
          ) : null}
          {entry.description_tr ? null : (
            <div className="text-ink-muted mt-0.5 line-clamp-2 max-w-md text-xs">
              {entry.description ?? ""}
            </div>
          )}
          <AiTags tags={entry.ai_tags} evidence={entry.ai_evidence} category={entry.category} />
        </td>
        <td className="tabular text-ink-secondary py-2.5 pr-4 text-right align-top text-xs">
          {days(entry.age_days)}
        </td>
        <td className="tabular py-2.5 pr-4 text-right align-top">{compact(entry.stars)}</td>
        <td className="tabular py-2.5 pr-4 text-right align-top">
          {entry.gain_1d === null ? "—" : `${entry.gain_1d > 0 ? "+" : ""}${count(entry.gain_1d)}`}
        </td>
        <td className="tabular py-2.5 pr-4 text-right align-top">
          <span className="text-ink font-medium">{rate(entry.velocity)}</span>
          {entry.window_days ? (
            <div className="text-ink-muted text-[11px]">{entry.window_days} günde</div>
          ) : (
            <div className="text-ink-muted text-[11px]" title="Karşılaştırılacak daha eski bir ölçüm yok">
              ölçülemedi
            </div>
          )}
        </td>
        <td className="tabular text-ink-secondary py-2.5 pr-4 text-right align-top">
          {rate(entry.lifetime_velocity)}
        </td>
      </tr>
      {prose ? (
        <tr className="border-border/60 border-b last:border-0">
          <td colSpan={8} className="px-4 pt-0 pb-4">
            <div className="sticky left-4 w-[calc(100vw-3rem)] md:static md:w-auto">
              <RepoProse entry={entry} />
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}
