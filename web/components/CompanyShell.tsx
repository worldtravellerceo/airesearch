import Link from "next/link";

import { CompanyTable } from "@/components/CompanyTable";
import { getCompanyBoard, getCompanyBoards } from "@/lib/data";
import { count, shortDate } from "@/lib/format";

/** The company side of the index.
 *
 *  The tabs come from the manifest rather than from a list in the code: a
 *  board is only written when it has rows, so the site offers exactly the
 *  questions the data can currently answer. A tab that is always there and
 *  sometimes empty claims the question was asked and came back blank.
 */
export async function CompanyShell({ slug }: { slug: string }) {
  const [boards, file] = await Promise.all([getCompanyBoards(), getCompanyBoard(slug)]);

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">
          Yapay zeka şirketleri
        </h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          İkinci evren. Repo tarafı bir projenin kaç yıldız aldığını ölçüyor; burası
          şirketin parasını, trafiğini ve puanını. İki listeyi birbirine bağlayan şey
          şirketin kendi alan adı.
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
                  // Bare, not prefixed: Next's `basePath` already adds
                  // /airesearch to every <Link>. Writing it again produced
                  // /airesearch/airesearch/... and 404'd every link on every
                  // company page — the whole section was unnavigable.
                  href={`/companies/${board.slug}/`}
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
              <p className="text-ink-secondary max-w-3xl text-sm">{file.blurb}</p>
              <CompanyTable slug={slug} entries={file.entries} />
              <p className="text-ink-muted text-xs">
                Güncelleme: {shortDate(file.as_of)}. Trafik rakamları Similarweb
                tahminidir; yatırım ve satın alma verisi Crunchbase&apos;ten, puanlar
                G2&apos;den gelir.
              </p>
            </>
          ) : (
            <p className="text-ink-secondary max-w-2xl py-8 text-sm">
              Bu liste için henüz veri toplanmadı. Sayfa duruyor ki eski bir bağlantı
              kırılmasın; sekmelerde yalnızca dolu olanlar görünüyor.
            </p>
          )}
        </>
      ) : (
        <p className="text-ink-secondary max-w-2xl py-8 text-sm">
          Şirket verisi henüz toplanmadı. Bu sayfa, kaynaklar ilk kez çalıştığında
          dolacak — o zamana kadar boş bir tablo göstermektense böyle söylemek daha
          doğru.
        </p>
      )}
    </div>
  );
}
