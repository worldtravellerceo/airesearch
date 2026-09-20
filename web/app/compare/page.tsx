import Link from "next/link";

import { CompareChart } from "@/components/CompareChart";
import { api, ApiError, type CompareSeries } from "@/lib/api";

export const revalidate = 900;

type SearchParams = Promise<{ repos?: string }>;

export default async function ComparePage({ searchParams }: { searchParams: SearchParams }) {
  const params = await searchParams;
  const requested = (params.repos ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
    .slice(0, 4);

  let series: CompareSeries[] = [];
  let error: string | null = null;

  if (requested.length) {
    try {
      series = (await api.compare(requested, "all")).series;
    } catch (cause) {
      error =
        cause instanceof ApiError && cause.status === 404
          ? "Bu projelerin hiçbiri takip edilmiyor."
          : "Karşılaştırma verisi alınamadı.";
    }
  }

  const missing = requested.filter(
    (name) => !series.some((s) => s.repo.toLowerCase() === name.toLowerCase()),
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-ink text-2xl font-semibold tracking-tight">Karşılaştır</h1>
        <p className="text-ink-secondary mt-1 max-w-3xl text-sm">
          En fazla dört projeyi aynı eksende görün. Varsayılan eksen takvim değil,
          her projenin kendi yaşı.
        </p>
      </div>

      <form action="/compare" method="get" className="flex flex-wrap gap-2">
        <label htmlFor="repos" className="sr-only">
          Projeler, virgülle ayrılmış
        </label>
        <input
          id="repos"
          name="repos"
          defaultValue={requested.join(",")}
          placeholder="owner/name, owner/name"
          className="border-border bg-surface-1 text-ink placeholder:text-ink-muted min-w-80 flex-1 rounded-md border px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="border-border bg-surface-2 text-ink hover:border-border-strong rounded-md border px-4 py-2 text-sm font-medium"
        >
          Karşılaştır
        </button>
      </form>

      {error ? (
        <p className="border-border text-ink rounded-lg border border-dashed p-6 text-center text-sm">
          {error}
        </p>
      ) : null}

      {missing.length && series.length ? (
        <p className="text-ink-muted text-xs">
          Takip edilmeyenler atlandı: {missing.join(", ")}
        </p>
      ) : null}

      {series.length ? (
        <section className="bg-surface-1 border-border rounded-lg border p-4">
          <CompareChart series={series} />
        </section>
      ) : !error ? (
        <p className="text-ink-muted border-border rounded-lg border border-dashed p-8 text-center text-sm">
          Karşılaştırmak için proje adı girin, ya da{" "}
          <Link href="/" className="text-accent hover:underline">
            board&apos;lardan
          </Link>{" "}
          seçin.
        </p>
      ) : null}
    </div>
  );
}
