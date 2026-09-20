import { dataUrl, repoSlug } from "@/lib/paths";
import type { HistoryPoint, RepoDetail } from "@/lib/types";

export type CompareSeries = {
  repo: string;
  created_at: string | null;
  stars: number;
  category: string | null;
  points: Array<HistoryPoint & { day_index: number | null }>;
};

/** Load one repo's curve for comparison.
 *
 *  Two fetches, because the metadata and the curve are published separately —
 *  the curve is the big half and most pages never need it.
 *
 *  `day_index` — days since the project's own creation — is what makes the
 *  comparison mean anything: on a calendar axis a five-year-old project and a
 *  two-week-old one barely overlap.
 */
export async function loadSeries(fullName: string): Promise<CompareSeries | null> {
  const { owner, name } = repoSlug(fullName);
  if (!owner || !name) return null;

  try {
    const [detailResponse, historyResponse] = await Promise.all([
      fetch(dataUrl("repos", owner, `${name}.json`)),
      fetch(dataUrl("repos", owner, `${name}.history.json`)),
    ]);
    if (!detailResponse.ok || !historyResponse.ok) return null;

    const detail: RepoDetail = await detailResponse.json();
    const history: HistoryPoint[] = (await historyResponse.json()).points ?? [];
    const created = detail.created_at ? new Date(detail.created_at) : null;

    return {
      repo: detail.full_name,
      created_at: detail.created_at,
      stars: detail.stars,
      category: detail.category,
      points: history.map((point) => ({
        ...point,
        day_index: created
          ? Math.round(
              (new Date(point.date).getTime() - created.getTime()) / 86_400_000,
            )
          : null,
      })),
    };
  } catch {
    return null;
  }
}
