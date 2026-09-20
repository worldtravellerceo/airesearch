import Link from "next/link";
import { notFound } from "next/navigation";

import { MilestoneStrip } from "@/components/BoardTable";
import { StarHistoryChart } from "@/components/StarHistoryChart";
import { StatTile } from "@/components/StatTile";
import { getManifest, getRepo } from "@/lib/data";
import { categoryLabel, count, multiple, percent, rate, shortDate } from "@/lib/format";
import { repoSlug } from "@/lib/paths";
import { BOARD_COPY, type Board, type RepoDetail } from "@/lib/types";

export const dynamic = "force-static";
// A repo that was exported yesterday but not today would otherwise 404 the whole
// build; the manifest is the list of pages that actually have data.
export const dynamicParams = false;

export async function generateStaticParams() {
  const manifest = await getManifest();
  const params = manifest.repos.map((fullName) => repoSlug(fullName));
  // A static export refuses to build a dynamic route that generates no routes
  // at all, and the very first deploy happens before any data exists. One
  // placeholder keeps the build honest instead of failing it.
  return params.length ? params : [PLACEHOLDER];
}

const PLACEHOLDER = { owner: "_", name: "_" };

export default async function RepoPage({
  params,
}: {
  params: Promise<{ owner: string; name: string }>;
}) {
  const { owner, name } = await params;
  const detail = await getRepo(owner, name);
  if (!detail) {
    if (owner === PLACEHOLDER.owner && name === PLACEHOLDER.name) return <NoData />;
    notFound();
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-ink-muted hover:text-ink text-sm">
          ← Board&apos;lar
        </Link>
        <h1 className="text-ink mt-2 text-2xl font-semibold tracking-tight">
          {detail.full_name}
          {detail.breakout ? <span className="ml-2">🔥</span> : null}
        </h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          {detail.one_liner ?? detail.description ?? "Açıklama yok."}
        </p>
        <div className="text-ink-muted mt-2 flex flex-wrap items-center gap-3 text-xs">
          <span>{categoryLabel(detail.category)}</span>
          {detail.language ? <span>{detail.language}</span> : null}
          {detail.license ? <span>{detail.license}</span> : null}
          <span>{shortDate(detail.created_at)} tarihinde oluşturuldu</span>
          <a
            href={`https://github.com/${detail.full_name}`}
            className="text-accent hover:underline"
            rel="noreferrer noopener"
            target="_blank"
          >
            GitHub&apos;da aç
          </a>
          <Link
            href={`/compare/?repos=${encodeURIComponent(detail.full_name)}`}
            className="text-accent hover:underline"
          >
            Karşılaştır
          </Link>
        </div>
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Toplam yıldız" value={count(detail.stars)} />
        <StatTile
          label="14 günlük hız"
          value={rate(detail.velocity_14d)}
          hint={`90 günlük ortalama ${rate(detail.velocity_90d)}`}
        />
        <StatTile
          label="İvme"
          value={multiple(detail.acceleration)}
          hint="kendi 90 günlük temposuna göre"
          tone={
            detail.acceleration && detail.acceleration >= 3
              ? "good"
              : detail.acceleration && detail.acceleration < 0.5
                ? "critical"
                : "neutral"
          }
        />
        <StatTile
          label="Fresh Power"
          value={count(detail.fresh_power)}
          hint="yaşa göre sönümlenmiş yıldız"
        />
      </section>

      <section className="bg-surface-1 border-border rounded-lg border p-4">
        <h2 className="text-ink mb-1 text-sm font-medium">Yıldız geçmişi</h2>
        <p className="text-ink-muted mb-3 text-xs">
          Toplam yıldız, kilometre taşlarıyla. Eski dönem haftalık, son{" "}
          {count(detail.coverage_days)} gün günlük çözünürlükte.
        </p>
        <StarHistoryChart
          points={detail.history}
          milestones={buildMilestones(detail)}
        />
      </section>

      <section className="grid gap-4 md:grid-cols-2">
        <div className="bg-surface-1 border-border rounded-lg border p-4">
          <h2 className="text-ink mb-3 text-sm font-medium">Kaç günde ulaştı</h2>
          <MilestoneStrip entry={detail} />
        </div>
        <div className="bg-surface-1 border-border rounded-lg border p-4">
          <h2 className="text-ink mb-3 text-sm font-medium">Board sıraları</h2>
          {Object.keys(detail.ranks ?? {}).length ? (
            <dl className="flex flex-wrap gap-6">
              {Object.entries(detail.ranks).map(([board, rank]) => (
                <div key={board}>
                  <dt className="text-ink-muted text-xs">
                    {BOARD_COPY[board as Board]?.title ?? board}
                  </dt>
                  <dd className="tabular text-ink text-lg font-semibold">#{rank}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="text-ink-muted text-sm">
              Hiçbir board&apos;un ilk 200&apos;ünde değil.
            </p>
          )}
          <dl className="text-ink-secondary mt-4 space-y-1 text-xs">
            <div className="flex gap-2">
              <dt className="text-ink-muted">14 günlük göreli büyüme</dt>
              <dd className="tabular">{percent(detail.relative_growth_14d)}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-ink-muted">Zirve hızı</dt>
              <dd className="tabular">
                {rate(detail.peak_velocity)}
                {detail.days_since_peak !== null && detail.days_since_peak !== undefined
                  ? ` · ${count(detail.days_since_peak)} gün önce`
                  : ""}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-ink-muted">Keşif kanalı</dt>
              <dd>{detail.discovered_via ?? "—"}</dd>
            </div>
          </dl>
        </div>
      </section>

      {detail.topics?.length ? (
        <section>
          <h2 className="text-ink-muted mb-2 text-xs font-medium tracking-wide uppercase">
            Topic&apos;ler
          </h2>
          <div className="flex flex-wrap gap-1.5">
            {detail.topics.map((topic) => (
              <span
                key={topic}
                className="border-border text-ink-secondary rounded-full border px-2.5 py-0.5 text-xs"
              >
                {topic}
              </span>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function NoData() {
  return (
    <div className="border-border rounded-lg border border-dashed p-12 text-center">
      <h1 className="text-ink text-lg font-semibold">Henüz veri yok</h1>
      <p className="text-ink-muted mt-2 text-sm">
        İlk toplama turu çalıştığında proje sayfaları burada görünecek.
      </p>
      <Link href="/" className="text-accent mt-4 inline-block text-sm hover:underline">
        Board&apos;lara dön
      </Link>
    </div>
  );
}

function buildMilestones(detail: RepoDetail) {
  if (!detail.created_at || !detail.history.length) return [];
  const created = new Date(detail.created_at);
  const available = new Set(detail.history.map((point) => point.date));

  return (
    [
      ["1k", detail.days_to_1k],
      ["10k", detail.days_to_10k],
      ["50k", detail.days_to_50k],
    ] as Array<[string, number | null]>
  )
    .filter(([, value]) => value !== null)
    .map(([label, value]) => {
      const date = new Date(created);
      date.setDate(date.getDate() + (value as number));
      return { label, date: date.toISOString().slice(0, 10) };
    })
    // The old part of the curve is weekly, so a milestone rarely lands on a
    // point that exists. Snap it to the nearest one we actually drew.
    .map((milestone) => ({
      ...milestone,
      date: available.has(milestone.date)
        ? milestone.date
        : nearest(detail.history.map((p) => p.date), milestone.date),
    }))
    .filter((milestone) => milestone.date !== null) as Array<{
    label: string;
    date: string;
  }>;
}

function nearest(dates: string[], target: string): string | null {
  const wanted = new Date(target).getTime();
  let best: string | null = null;
  let bestGap = Infinity;
  for (const date of dates) {
    const gap = Math.abs(new Date(date).getTime() - wanted);
    if (gap < bestGap) {
      bestGap = gap;
      best = date;
    }
  }
  // More than a fortnight away means the milestone predates what we drew.
  return bestGap <= 14 * 86_400_000 ? best : null;
}
