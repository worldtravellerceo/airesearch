import Link from "next/link";

/** What a reader who lands here was actually told.
 *
 *  The old copy said the project was not in the tracked universe and might be
 *  under fifty stars. That was false for the most likely visitor: someone who
 *  clicked a row on a board the site had just rendered. Detail pages used to
 *  be the top 1,200 repositories by stars while three of the four boards rank
 *  by velocity, so 114 of Breakout's 132 rows landed here — and were told the
 *  project we had just ranked did not exist.
 *
 *  Pages are now written for everyone on a board, so this should be rare. When
 *  it does happen the honest answer is that the page was not written, not that
 *  the project is unknown.
 */
export default function NotFound() {
  return (
    <div className="border-border rounded-lg border border-dashed p-12 text-center">
      <h1 className="text-ink text-lg font-semibold">Bu sayfa yok</h1>
      <p className="text-ink-secondary mx-auto mt-2 max-w-md text-sm">
        Aradığın proje endekste olabilir — ama her proje için ayrı bir sayfa yazılmıyor.
        Sayfalar, board&apos;larda görünen projeler ve en çok yıldız alanlar için üretiliyor.
      </p>
      <p className="text-ink-muted mx-auto mt-2 max-w-md text-xs">
        Arama kutusundan projeyi aratabilirsin; endekste varsa orada çıkar.
      </p>
      <Link href="/" className="text-accent mt-4 inline-block text-sm hover:underline">
        Board&apos;lara dön
      </Link>
    </div>
  );
}
