import Link from "next/link";

import { ExplosionTable } from "@/components/ExplosionTable";
import { getExplosionBoard, getExplosionBoards } from "@/lib/data";
import { count, percent, shortDate } from "@/lib/format";
import type { BoardMovement, ExplosionLevel } from "@/lib/types";

/** How many rows the build inlines. The rest are fetched on request. */
const INLINE_ROWS = 100;

/** Every young project above the explosion level, AI or not.
 *
 *  The other boards rank the AI universe, decided by a classifier — the right
 *  gate for "what is happening in AI", and the reason `storytold/photocraft`
 *  went from 2,818 to 31,865 stars in three days without appearing anywhere
 *  here: it is an image editor. The reader asked to see everything that
 *  explodes and to be told how it relates to AI, not to have it filtered out.
 */
export async function ExplosionShell({ slug }: { slug: string }) {
  const [boards, file] = await Promise.all([getExplosionBoards(), getExplosionBoard(slug)]);

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">Patlayanlar</h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          GitHub&apos;da son üç ayda açılmış ve patlama seviyesini geçmiş her proje — web
          uygulaması, mobil, masaüstü, araç; AI olsun olmasın. AI ile ilişkisi bir etiket,
          filtre değil: kaçırmaktansa fazlası.
        </p>
      </section>

      {boards.length ? (
        <>
          <nav className="border-border flex flex-wrap gap-1 border-b">
            {boards.map((board) => {
              const active = board.slug === slug;
              return (
                <Link
                  key={board.slug}
                  href={`/patlayanlar/${board.slug}/`}
                  aria-current={active ? "page" : undefined}
                  className={
                    active
                      ? "border-ink text-ink -mb-px border-b-2 px-3 py-2 text-sm font-medium"
                      : "text-ink-secondary hover:text-ink -mb-px border-b-2 border-transparent px-3 py-2 text-sm"
                  }
                >
                  {board.title}
                  <span className="text-ink-muted ml-1.5 text-xs">{count(board.count)}</span>
                </Link>
              );
            })}
          </nav>

          {file ? (
            <>
              <div className="space-y-2">
                <p className="text-ink-secondary max-w-3xl text-sm">{file.blurb}</p>
                <Level level={file.level} />
                <Movement movement={file.movement} />
              </div>
              <ExplosionTable
                slug={slug}
                initial={file.entries.slice(0, INLINE_ROWS)}
                total={file.total}
              />
              <p className="text-ink-muted max-w-3xl text-xs">
                Güncelleme: {shortDate(file.as_of)}. Yıldızlar GitHub aramasının günlük
                sayımından (census) geliyor; tam yıldız geçmişi yalnızca AI evreni için
                toplanıyor. Bu liste filtrelenmiyor ve yıldız sayısı güvenlik kanıtı
                değildir: kırık yazılım, aktivasyon aracı ya da ücretli bir programın
                &quot;bedava&quot; sürümünü vaat eden repolar çoğu zaman zararlı yazılım yemidir.
              </p>
            </>
          ) : (
            <p className="text-ink-secondary max-w-2xl py-8 text-sm">
              Bu liste bugün boş. Sayfa duruyor ki eski bir bağlantı kırılmasın; sekmelerde
              yalnızca dolu olanlar görünüyor.
            </p>
          )}
        </>
      ) : (
        <p className="text-ink-secondary max-w-2xl py-8 text-sm">
          Patlama listesi henüz üretilmedi. Günlük tur ilk kez çalıştığında burası dolacak.
        </p>
      )}
    </div>
  );
}

function Level({ level }: { level: ExplosionLevel }) {
  return (
    <p className="text-ink-muted max-w-3xl text-xs">
      Eşik: son {level.max_age_days} günde açılmış ve {count(level.min_stars_outright)} yıldızı
      geçmiş — ya da en az {count(level.min_stars)} yıldızla son günlerde günde{" "}
      {count(level.min_window_velocity)}+ ya da açıldığından beri günde ortalama{" "}
      {count(level.min_lifetime_velocity)}+ yıldız almış. Daha eskiler &quot;Yeniden
      patlayanlar&quot;da: iki haftada {count(level.resurgent_min_gain)}+ yıldız ve{" "}
      {percent(level.resurgent_min_growth)}+ büyüme.
    </p>
  );
}

function Movement({ movement }: { movement?: BoardMovement | null }) {
  if (!movement || !movement.compared) return null;
  return (
    <p className="text-ink-secondary flex flex-wrap gap-x-3 gap-y-1 text-xs">
      <span className="text-ink-muted">
        {movement.since ? `${movement.since} karşılaştırması:` : "önceki tura göre:"}
      </span>
      {movement.entered ? (
        <span className="text-ink font-medium">{movement.entered} yeni giriş</span>
      ) : null}
      {movement.moved_far ? <span>{movement.moved_far} proje 5+ sıra oynadı</span> : null}
    </p>
  );
}
