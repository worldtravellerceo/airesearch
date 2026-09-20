"use client";

import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { HistoryPoint } from "@/lib/types";
import { dateTickFormatter, dateTicks } from "@/lib/axis";
import { compact, count, shortDate } from "@/lib/format";

type Milestone = { label: string; date: string };

/** One repo's star history: cumulative total as the line, with milestone
 *  markers. A single series, so it carries no legend — the heading names it. */
export function StarHistoryChart({
  points,
  milestones = [],
}: {
  points: HistoryPoint[];
  milestones?: Milestone[];
}) {
  if (points.length < 2) {
    return (
      <p className="text-ink-muted py-8 text-center text-sm">
        Grafiği çizecek kadar geçmiş toplanmadı.
      </p>
    );
  }

  const formatTick = dateTickFormatter(points);
  const ticks = dateTicks(points);

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        {/* The top margin leaves room for milestone labels, which sit above the
            plot area and would otherwise be clipped by the SVG edge. */}
        <ComposedChart data={points} margin={{ top: 22, right: 12, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--border)" }}
            minTickGap={40}
            ticks={ticks}
            tickFormatter={formatTick}
          />
          <YAxis
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(value: number) => compact(value)}
          />
          <Tooltip
            cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
            content={<HistoryTooltip />}
          />
          <Area
            type="monotone"
            dataKey="cumulative"
            stroke="none"
            fill="var(--series-1)"
            fillOpacity={0.12}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="cumulative"
            stroke="var(--series-1)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          {milestones.map((milestone) => (
            <ReferenceLine
              key={milestone.label}
              x={milestone.date}
              stroke="var(--border-strong)"
              strokeDasharray="3 3"
              label={{
                value: milestone.label,
                position: "top",
                offset: 8,
                fill: "var(--text-muted)",
                fontSize: 10,
              }}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function HistoryTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ payload: HistoryPoint }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="bg-surface-1 border-border rounded-md border px-3 py-2 text-xs shadow-lg">
      <div className="text-ink-secondary">{shortDate(label)}</div>
      <div className="tabular text-ink mt-1 font-semibold">
        {count(point.cumulative)} toplam
      </div>
      <div className="tabular text-ink-secondary">
        o gün +{count(point.stars_gained)}
      </div>
    </div>
  );
}
