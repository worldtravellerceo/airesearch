import Link from "next/link";

export default function NotFound() {
  return (
    <div className="border-border rounded-lg border border-dashed p-12 text-center">
      <h1 className="text-ink text-lg font-semibold">Bulunamadı</h1>
      <p className="text-ink-muted mt-2 text-sm">
        Bu proje izlenen evrende değil — keşif eşiğinin (50 yıldız) altında olabilir.
      </p>
      <Link href="/" className="text-accent mt-4 inline-block text-sm hover:underline">
        Board&apos;lara dön
      </Link>
    </div>
  );
}
