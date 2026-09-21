import Link from "next/link";

import { compact, count, percent, roundLabel, shortDate, usd } from "@/lib/format";
import type { CompanyEntry } from "@/lib/types";

type Column = {
  key: string;
  label: string;
  right?: boolean;
  render: (entry: CompanyEntry) => React.ReactNode;
  /** Shown on hover where the number needs a caveat the header cannot hold. */
  title?: string;
};

function domainOf(entry: CompanyEntry): string | null {
  return entry.domain ?? entry.company_domain ?? null;
}

/** The company, with its site and — where we have one — the repository that
 *  put it in this index in the first place. That link is the whole reason for
 *  having both universes: neither source knows both halves. */
function Company({ entry }: { entry: CompanyEntry }) {
  const domain = domainOf(entry);
  const label = entry.name || entry.company_name || domain || "—";
  return (
    <div className="min-w-0">
      <div className="text-ink truncate font-medium">{label}</div>
      <div className="text-ink-muted flex gap-2 truncate text-xs">
        {domain ? (
          <a
            href={`https://${domain}`}
            target="_blank"
            rel="noopener noreferrer"
            className="hover:text-ink-secondary"
          >
            {domain}
          </a>
        ) : null}
        {entry.top_repo ? (
          <Link
            href={`/repos/${entry.top_repo}/`}
            className="hover:text-ink-secondary"
          >
            {entry.top_repo}
            {entry.repo_stars ? ` · ${compact(entry.repo_stars)}★` : ""}
          </Link>
        ) : null}
      </div>
    </div>
  );
}

const COMPANY: Column = {
  key: "company",
  label: "Şirket",
  render: (entry) => <Company entry={entry} />,
};

/** Columns per board. They differ because the boards answer different
 *  questions — a shared table would have to show an empty cell for most of
 *  them, and an empty cell reads as missing data rather than as not applicable. */
export const COLUMNS: Record<string, Column[]> = {
  funded: [
    COMPANY,
    { key: "round_type", label: "Tur", render: (e) => roundLabel(e.round_type) },
    {
      key: "amount_usd",
      label: "Tutar",
      right: true,
      title: "Açıklanmayan tutarlar boş bırakılıyor, sıfır yazılmıyor.",
      render: (e) => usd(e.amount_usd),
    },
    { key: "announced_on", label: "Tarih", render: (e) => shortDate(e.announced_on) },
    {
      key: "investors",
      label: "Yatırımcılar",
      render: (e) => <span className="text-ink-secondary text-xs">{e.investors ?? "—"}</span>,
    },
  ],
  valuation: [
    COMPANY,
    { key: "valuation_usd", label: "Değerleme", right: true, render: (e) => usd(e.valuation_usd) },
    { key: "total_usd", label: "Toplanan", right: true, render: (e) => usd(e.total_usd) },
    { key: "valuation_on", label: "Tarih", render: (e) => shortDate(e.valuation_on) },
    {
      key: "valuation_src",
      label: "Kaynak",
      title: "Her değerleme basında geçtiği haberle birlikte saklanıyor — ölçülmüş değil.",
      render: (e) =>
        e.valuation_src ? (
          <a
            href={e.valuation_src}
            target="_blank"
            rel="noopener noreferrer"
            className="text-ink-secondary text-xs underline underline-offset-2"
          >
            haber
          </a>
        ) : (
          "—"
        ),
    },
  ],
  raised: [
    COMPANY,
    { key: "total_usd", label: "Toplam", right: true, render: (e) => usd(e.total_usd) },
    { key: "rounds", label: "Tur", right: true, render: (e) => count(e.rounds) },
    { key: "last_round", label: "Son tur", render: (e) => roundLabel(e.last_round) },
    { key: "last_round_on", label: "Tarih", render: (e) => shortDate(e.last_round_on) },
    { key: "employee_range", label: "Çalışan", render: (e) => e.employee_range ?? "—" },
    { key: "country", label: "Ülke", render: (e) => e.country ?? "—" },
  ],
  acquired: [
    { key: "target", label: "Alınan", render: (e) => e.target ?? "—" },
    { key: "acquirer", label: "Alan", render: (e) => e.acquirer ?? "—" },
    { key: "announced_on", label: "Tarih", render: (e) => shortDate(e.announced_on) },
    { key: "amount_usd", label: "Tutar", right: true, render: (e) => usd(e.amount_usd) },
  ],
  traffic: [
    COMPANY,
    {
      key: "visits",
      label: "Aylık ziyaret",
      right: true,
      title: "Similarweb tahmini. Ölçüm değil.",
      render: (e) => compact(e.visits),
    },
    { key: "global_rank", label: "Dünya sırası", right: true, render: (e) => count(e.global_rank) },
    {
      key: "traffic_genai",
      label: "AI'dan",
      right: true,
      render: (e) => percent(e.traffic_genai),
    },
    { key: "month", label: "Ay", render: (e) => shortDate(e.month) },
  ],
  rising: [
    COMPANY,
    {
      key: "growth",
      label: "Büyüme",
      right: true,
      title: "İki ay arasındaki oran. Üç aylık veriden fazlası yok.",
      render: (e) => (e.growth ? `${e.growth.toFixed(2)}×` : "—"),
    },
    { key: "prev_visits", label: "Önceki ay", right: true, render: (e) => compact(e.prev_visits) },
    { key: "visits", label: "Son ay", right: true, render: (e) => compact(e.visits) },
    { key: "month", label: "Ay", render: (e) => shortDate(e.month) },
  ],
  "ai-traffic": [
    COMPANY,
    {
      key: "traffic_genai",
      label: "AI payı",
      right: true,
      title: "Ziyaretlerin ChatGPT, Claude, Gemini ve Perplexity'den gelen kısmı.",
      render: (e) => percent(e.traffic_genai),
    },
    { key: "visits", label: "Aylık ziyaret", right: true, render: (e) => compact(e.visits) },
    { key: "month", label: "Ay", render: (e) => shortDate(e.month) },
  ],
  rated: [
    COMPANY,
    {
      key: "avg_rating",
      label: "Puan",
      right: true,
      render: (e) => (e.avg_rating ? `${e.avg_rating.toFixed(2)} / 5` : "—"),
    },
    { key: "reviews", label: "İnceleme", right: true, render: (e) => count(e.reviews) },
    { key: "product_slug", label: "G2 ürünü", render: (e) => e.product_slug ?? "—" },
  ],
};

export function CompanyTable({ slug, entries }: { slug: string; entries: CompanyEntry[] }) {
  const columns = COLUMNS[slug];
  if (!columns) return null;

  if (!entries.length) {
    return (
      <p className="text-ink-secondary py-8 text-sm">
        Bu listede henüz kayıt yok.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[42rem] text-sm">
        <thead className="text-ink-muted border-border border-b text-left text-xs">
          <tr>
            <th scope="col" className="w-10 py-2.5 pr-3 font-medium">
              #
            </th>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                title={column.title}
                className={`py-2.5 pr-4 font-medium ${column.right ? "text-right" : ""}`}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-border divide-y">
          {entries.map((entry, index) => (
            <tr key={`${slug}-${index}`} className="hover:bg-surface-2/60">
              <td className="text-ink-muted py-2.5 pr-3 tabular-nums">{index + 1}</td>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`py-2.5 pr-4 ${column.right ? "text-right tabular-nums" : ""}`}
                >
                  {column.render(entry)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
