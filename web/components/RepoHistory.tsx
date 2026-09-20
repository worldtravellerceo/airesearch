"use client";

import { useEffect, useMemo, useState } from "react";

import { StarHistoryChart } from "@/components/StarHistoryChart";
import { dataUrl } from "@/lib/paths";
import type { HistoryPoint } from "@/lib/types";

export type Milestone = { label: string; date: string };

// The old part of a curve is weekly, so a milestone rarely lands exactly on a
// point that was drawn. Anything further away than this predates the data.
const SNAP_WINDOW_MS = 14 * 86_400_000;

/** Fetches a repo's star curve and draws it.
 *
 *  The curve lives in its own file rather than in the page. A lifetime history
 *  is the largest thing about a repo, and a static build inlines whatever a
 *  page reads into both its HTML and its client payload — inlining it cost
 *  about 125 KB per page, twice, for a chart below the fold.
 */
export function RepoHistory({
  owner,
  name,
  milestones,
  expectedPoints,
}: {
  owner: string;
  name: string;
  /** Raw milestone dates, derived from the repo's creation date. Snapping them
   *  onto the drawn series happens here, where the points are. */
  milestones: Milestone[];
  expectedPoints: number;
}) {
  const [points, setPoints] = useState<HistoryPoint[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(dataUrl("repos", owner, `${name}.history.json`))
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then((data) => {
        if (!cancelled) setPoints(data.points ?? []);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [owner, name]);

  const snapped = useMemo(() => snap(milestones, points ?? []), [milestones, points]);

  if (failed) {
    return (
      <p className="text-ink-muted py-8 text-center text-sm">
        Yıldız geçmişi yüklenemedi.
      </p>
    );
  }
  if (!points) {
    // Reserve the chart's height so the rest of the page does not jump.
    return (
      <div className="text-ink-muted flex h-72 items-center justify-center text-sm">
        {expectedPoints > 0 ? "Grafik yükleniyor…" : "Henüz geçmiş toplanmadı."}
      </div>
    );
  }
  return <StarHistoryChart points={points} milestones={snapped} />;
}

function snap(milestones: Milestone[], points: HistoryPoint[]): Milestone[] {
  if (!points.length) return [];
  const dates = points.map((point) => point.date);
  const available = new Set(dates);

  return milestones
    .map((milestone) => {
      if (available.has(milestone.date)) return milestone;
      const wanted = new Date(milestone.date).getTime();
      let best: string | null = null;
      let bestGap = Infinity;
      for (const date of dates) {
        const gap = Math.abs(new Date(date).getTime() - wanted);
        if (gap < bestGap) {
          bestGap = gap;
          best = date;
        }
      }
      // A reference line on a date the series does not contain renders nowhere,
      // so a milestone that predates the drawn curve is dropped rather than
      // pinned to its start, where it would claim something untrue.
      return bestGap <= SNAP_WINDOW_MS && best
        ? { ...milestone, date: best }
        : null;
    })
    .filter((milestone): milestone is Milestone => milestone !== null);
}
