import Link from "next/link";

import { AiTags } from "@/components/AiTags";
import { getDigest } from "@/lib/data";
import { categoryLabel, count, rate, ratio, shortDate } from "@/lib/format";
import type { DigestArrival, DigestExplosions, DigestMover, DigestWarming } from "@/lib/types";

export const dynamic = "force-static";

export const metadata = {
  title: "Bugün — AI Radar",
  description: "Dün akşamdan bu sabaha ne girdi, ne kıpırdadı.",
};

/** The morning read.
 *
 *  The four boards answer "what is big" and "what is moving". None of them
 *  answers "what is new since I last looked", and that question had no page:
 *  `first_seen_at` was recorded for every repository from the first day and
 *  read by nothing. An arrival at 10,351 stars appeared somewhere in the middle
 *  of a board with no mark on it saying it had not been there yesterday.
 */
export default async function TodayPage() {
  const digest = await getDigest();
  const { new_projects: fresh, newly_tracked: crossed, movers, warming, counts } = digest;
  const exploded = digest.explosions;
  const nothing =
    !fresh.length &&
    !crossed.length &&
    !movers.length &&
    !warming.length &&
    !exploded?.entered.length;

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">Bugün</h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          {digest.date
            ? `${shortDate(digest.date)} itibarıyla: indekse ne girdi, hangi repo gün içinde en çok yıldız aldı.`
            : "Henüz bir tur bu dosyayı üretmedi."}
        </p>
      </section>

      {nothing ? (
        <p className="text-ink-secondary rounded-lg border border-[--color-rule] p-4 text-sm">
          {digest.date
            ? "Bu tur eşiğin üstünde yeni bir giriş bulmadı. Boş bir liste, yanlış bir listeden iyidir."
            : "Veri bekleniyor. Günlük tur bu dosyayı yazdığında burası dolacak."}
        </p>
      ) : null}

      {exploded?.entered.length ? <Explosions digest={exploded} /> : null}

      {fresh.length ? (
        <Arrivals
          title="Yeni projeler"
          note={`Son ${counts.arrival_min_stars}+ yıldızlı girişler arasında 90 günden genç olanlar.`}
          rows={fresh}
        />
      ) : null}

      {crossed.length ? (
        <Arrivals
          title="İndekse yeni girenler"
          note="Yeni değil ama eşiği şimdi geçtiler — uzun süredir var olup radara ilk kez takılanlar."
          rows={crossed}
        />
      ) : null}

      {warming.length ? <Warming rows={warming} /> : null}

      {movers.length ? <Movers rows={movers} /> : null}

      {counts.arrivals_total > counts.arrivals_shown ? (
        <p className="text-ink-secondary text-xs">
          Bugün toplam {count(counts.arrivals_total)} yeni repo girdi; burada {counts.arrival_min_stars}+
          yıldızlı {count(counts.arrivals_shown)} tanesi var. Eşik, listeyi okunabilir tutmak için:
          eşiksiz hali günde 50–520 satır.
        </p>
      ) : null}
    </div>
  );
}

/** Today's entries to the explosion list — every kind of project, AI or not.
 *
 *  First on the page because it is the only section here that is not gated on
 *  the classifier: everything below it is the AI universe. */
function Explosions({ digest }: { digest: DigestExplosions }) {
  const total = digest.entered_total ?? digest.entered.length;
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-ink text-lg font-semibold tracking-tight">
          Patlayanlar: bugün listeye girenler{" "}
          <span className="text-ink-secondary font-normal">({count(total)})</span>
        </h2>
        <p className="text-ink-secondary mt-0.5 text-xs">
          Son {digest.level.max_age_days} günde açılmış, patlama seviyesini{" "}
          {digest.since ? `${shortDate(digest.since)} turundan bu yana` : "bu turda"} geçen projeler
          — AI olsun olmasın. Listede toplam {count(digest.total)} proje var.{" "}
          <Link href="/patlayanlar/bugun-girenler/" className="text-accent hover:underline">
            Tamamı →
          </Link>
        </p>
      </div>
      <ul className="space-y-2">
        {digest.entered.map((row) => (
          <li key={row.full_name} className="rounded-lg border border-[--color-rule] p-3">
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              {row.has_page ? (
                <Link href={`/repos/${row.full_name}/`} className="text-ink font-medium hover:underline">
                  {row.full_name}
                </Link>
              ) : (
                <a
                  href={`https://github.com/${row.full_name}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-ink font-medium hover:underline"
                >
                  {row.full_name}
                </a>
              )}
              <div className="text-ink-secondary flex items-baseline gap-3 text-sm tabular-nums">
                <span className="text-ink font-medium">{count(row.stars)}</span>
                <span>{rate(row.velocity ?? row.lifetime_velocity)}</span>
                {row.age_days === null ? null : <span>{count(row.age_days)} günlük</span>}
              </div>
            </div>
            {row.description_tr || row.description ? (
              <p className="text-ink-secondary mt-1 text-sm">{row.description_tr ?? row.description}</p>
            ) : null}
            <AiTags tags={row.ai_tags} evidence={null} category={row.category} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function Arrivals({
  title,
  note,
  rows,
}: {
  title: string;
  note: string;
  rows: DigestArrival[];
}) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-ink text-lg font-semibold tracking-tight">
          {title} <span className="text-ink-secondary font-normal">({rows.length})</span>
        </h2>
        <p className="text-ink-secondary mt-0.5 text-xs">{note}</p>
      </div>
      <ul className="space-y-3">
        {rows.map((row) => (
          <li
            key={row.full_name}
            className="rounded-lg border border-[--color-rule] p-4"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <Link
                href={`/repos/${row.full_name}/`}
                className="text-ink font-medium hover:underline"
              >
                {row.full_name}
              </Link>
              <div className="text-ink-secondary flex items-baseline gap-3 text-sm tabular-nums">
                <span className="text-ink font-medium">{count(row.stars)}</span>
                <span>{categoryLabel(row.category)}</span>
                {row.age_days === null ? null : <span>{count(row.age_days)} günlük</span>}
              </div>
            </div>
            {row.description ? (
              <p className="text-ink-secondary mt-1 text-sm">{row.description}</p>
            ) : null}
            {row.description_tr ? (
              <p className="text-ink mt-2 text-sm leading-relaxed">{row.description_tr}</p>
            ) : (
              <p className="text-ink-secondary mt-2 text-xs italic">
                Türkçe paragrafı henüz yazılmadı — bir sonraki tur yazar.
              </p>
            )}
            {row.usage_tr ? (
              <p className="text-ink-secondary mt-2 text-sm leading-relaxed">
                <span className="text-accent font-medium">
                  {row.matched_project ?? "Doğrudan eşleşme yok"}:{" "}
                </span>
                {row.usage_tr}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Warming({ rows }: { rows: DigestWarming[] }) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-ink text-lg font-semibold tracking-tight">Isınanlar</h2>
        <p className="text-ink-secondary mt-0.5 text-xs">
          Kendi son temposunun 1,5–3 katına çıkmış ama Breakout&apos;un 3 kat eşiğini henüz
          geçmemiş projeler. Hiçbir board bunları göstermiyor: Momentum mutlak hıza bakıyor, orada
          hep aynı büyükler var; Breakout ise eşiğin üstünü. Aradaki bant burası.
        </p>
      </div>
      <table className="w-full text-sm">
        <thead className="text-ink-secondary border-b border-[--color-rule] text-left text-xs">
          <tr>
            <th className="py-2 font-medium">repo</th>
            <th className="py-2 font-medium">kategori</th>
            <th className="py-2 text-right font-medium">ivme</th>
            <th className="py-2 text-right font-medium">hız</th>
            <th className="py-2 text-right font-medium">yıldız</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.full_name} className="border-b border-[--color-rule] last:border-0">
              <td className="py-2">
                <Link href={`/repos/${row.full_name}/`} className="text-ink hover:underline">
                  {row.full_name}
                </Link>
                {row.matched_project ? (
                  <span className="text-accent ml-2 text-xs">{row.matched_project}</span>
                ) : null}
              </td>
              <td className="text-ink-secondary py-2">{categoryLabel(row.category)}</td>
              <td className="text-ink py-2 text-right font-medium tabular-nums">
                {ratio(row.acceleration)}
              </td>
              <td className="text-ink-secondary py-2 text-right tabular-nums">
                {rate(row.velocity_14d)}
              </td>
              <td className="text-ink-secondary py-2 text-right tabular-nums">
                {count(row.stars)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Movers({ rows }: { rows: DigestMover[] }) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-ink text-lg font-semibold tracking-tight">Gün içinde en çok artanlar</h2>
        <p className="text-ink-secondary mt-0.5 text-xs">
          Tek günün yıldız artışı. Momentum bunu 14 güne yayıp ortalıyor — sıralama için doğrusu o,
          &ldquo;dün ne oldu&rdquo; için doğrusu bu.
        </p>
      </div>
      <table className="w-full text-sm">
        <thead className="text-ink-secondary border-b border-[--color-rule] text-left text-xs">
          <tr>
            <th className="py-2 font-medium">repo</th>
            <th className="py-2 font-medium">kategori</th>
            <th className="py-2 text-right font-medium">dün</th>
            <th className="py-2 text-right font-medium">toplam</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.full_name} className="border-b border-[--color-rule] last:border-0">
              <td className="py-2">
                <Link href={`/repos/${row.full_name}/`} className="text-ink hover:underline">
                  {row.full_name}
                </Link>
              </td>
              <td className="text-ink-secondary py-2">{categoryLabel(row.category)}</td>
              <td className="text-ink py-2 text-right font-medium tabular-nums">
                +{count(row.stars_gained)}
              </td>
              <td className="text-ink-secondary py-2 text-right tabular-nums">
                {count(row.stars)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
