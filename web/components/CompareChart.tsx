"use client";

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  LabelList,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { CompareSeries } from "@/lib/compare";
import { compact, count, shortDate } from "@/lib/format";

const SERIES_COLORS = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
];

type Axis = "age" | "date";

/** Several repos' star curves together.
 *
 *  The default x-axis is days since each project's own creation, not the
 *  calendar. On a calendar axis a five-year-old project and a two-week-old one
 *  barely overlap and the comparison says nothing; aligned at birth, "how fast
 *  did this thing actually grow" becomes readable at a glance.
 *
 *  Two of the four series colours sit below 3:1 contrast on the light surface,
 *  so the legend is always present and the table view is one click away. */
export function CompareChart({ series }: { series: CompareSeries[] }) {
  const [axis, setAxis] = useState<Axis>("age");
  const [showTable, setShowTable] = useState(false);

  const rows = useMemo(() => buildRows(series, axis), [series, axis]);
  const names = series.map((s) => s.repo);

  if (!series.length) {
    return <p className="text-ink-muted text-sm">Karşılaştırılacak proje seçilmedi.</p>;
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented
          value={axis}
          onChange={setAxis}
          options={[
            { value: "age", label: "Doğumdan itibaren gün" },
            { value: "date", label: "Takvim" },
          ]}
        />
        <button
          type="button"
          onClick={() => setShowTable((value) => !value)}
          className="border-border text-ink-secondary hover:bg-surface-2 ml-auto rounded-md border px-3 py-1.5 text-xs"
          aria-pressed={showTable}
        >
          {showTable ? "Grafiği göster" : "Tabloyu göster"}
        </button>
      </div>

      {axis === "age" ? (
        <p className="text-ink-muted mb-3 text-xs">
          Her proje kendi doğum gününden hizalandı — 5 yıllık bir projeyle 2 haftalık
          birini adil kıyaslamanın tek yolu bu.
        </p>
      ) : null}

      {showTable ? (
        <CompareTable series={series} />
      ) : (
        <div className="h-80 w-full">
          <ResponsiveContainer width="100%" height="100%">
            {/* The right margin holds the end-of-line labels: with four series the
                  legend alone is not enough, and two of the four colours sit below
                  3:1 on the light surface. */}
              <LineChart data={rows} margin={{ top: 8, right: 108, bottom: 4, left: 4 }}>
              <CartesianGrid stroke="var(--grid)" vertical={false} />
              <XAxis
                dataKey="x"
                type={axis === "age" ? "number" : "category"}
                domain={axis === "age" ? ["dataMin", "dataMax"] : undefined}
                tick={{ fill: "var(--text-muted)", fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: "var(--border)" }}
                minTickGap={48}
                tickFormatter={(value) =>
                  axis === "age"
                    ? `${compact(Number(value))} g`
                    : new Date(value).toLocaleDateString("tr-TR", {
                        month: "short",
                        year: "2-digit",
                      })
                }
              />
              <YAxis
                tick={{ fill: "var(--text-muted)", fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={56}
                tickFormatter={(value: number) => compact(value)}
              />
              <Tooltip
                cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
                content={<CompareTooltip axis={axis} />}
              />
              <Legend
                verticalAlign="top"
                align="left"
                height={28}
                iconType="plainline"
                wrapperStyle={{ fontSize: 12 }}
                // Labels wear the text token; the coloured line beside them
                // carries identity. Colouring the words instead makes them
                // harder to read and encodes identity in colour alone.
                formatter={(value) => (
                  <span style={{ color: "var(--text-secondary)" }}>{value}</span>
                )}
              />
              {names.map((name, index) => (
                <Line
                  key={name}
                  type="monotone"
                  dataKey={name}
                  name={name}
                  stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                >
                  <LabelList
                    dataKey={name}
                    content={(props) => (
                      <EndLabel
                        {...(props as EndLabelProps)}
                        name={name}
                        lastIndex={lastIndexFor(rows, name)}
                      />
                    )}
                  />
                </Line>
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

type EndLabelProps = {
  x?: number | string;
  y?: number | string;
  index?: number;
  value?: number | string;
};

/** A direct label at the end of one series' line.
 *
 *  Only rendered on that series' own last point, because each project's curve
 *  stops at a different x. */
function EndLabel({
  x,
  y,
  index,
  name,
  lastIndex,
}: EndLabelProps & { name: string; lastIndex: number }) {
  if (index !== lastIndex || x === undefined || y === undefined) return null;
  const short = name.split("/")[1] ?? name;
  return (
    <text
      x={Number(x) + 8}
      y={Number(y)}
      dy={4}
      fontSize={11}
      fill="var(--text-secondary)"
      textAnchor="start"
    >
      {short.length > 14 ? `${short.slice(0, 13)}…` : short}
    </text>
  );
}

function lastIndexFor(rows: Array<Record<string, number | string>>, name: string): number {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i][name] !== undefined) return i;
  }
  return -1;
}

function buildRows(series: CompareSeries[], axis: Axis) {
  const byX = new Map<number | string, Record<string, number | string>>();
  for (const entry of series) {
    for (const point of entry.points) {
      const x = axis === "age" ? (point.day_index ?? 0) : point.date;
      const row = byX.get(x) ?? { x };
      row[entry.repo] = point.cumulative;
      byX.set(x, row);
    }
  }
  return [...byX.values()].sort((a, b) =>
    typeof a.x === "number" && typeof b.x === "number"
      ? a.x - b.x
      : String(a.x).localeCompare(String(b.x)),
  );
}

function CompareTooltip({
  active,
  payload,
  label,
  axis,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color: string }>;
  label?: number | string;
  axis: Axis;
}) {
  if (!active || !payload?.length) return null;
  const sorted = [...payload].sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
  return (
    <div className="bg-surface-1 border-border rounded-md border px-3 py-2 text-xs shadow-lg">
      <div className="text-ink-secondary mb-1">
        {axis === "age" ? `${count(Number(label))}. gün` : shortDate(String(label))}
      </div>
      {sorted.map((item) => (
        <div key={item.name} className="flex items-center gap-2">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: item.color }}
            aria-hidden
          />
          <span className="text-ink-secondary">{item.name}</span>
          <span className="tabular text-ink ml-auto font-medium">{count(item.value)}</span>
        </div>
      ))}
    </div>
  );
}

function CompareTable({ series }: { series: CompareSeries[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-ink-muted border-border border-b text-left text-xs">
            <th className="py-2 pr-4 font-medium">Proje</th>
            <th className="py-2 pr-4 text-right font-medium">Toplam yıldız</th>
            <th className="py-2 pr-4 text-right font-medium">Yaş</th>
            <th className="py-2 text-right font-medium">Günlük ortalama</th>
          </tr>
        </thead>
        <tbody>
          {series.map((entry, index) => {
            const last = entry.points[entry.points.length - 1];
            const age = last?.day_index ?? null;
            return (
              <tr key={entry.repo} className="border-border/60 border-b last:border-0">
                <td className="py-2 pr-4">
                  <span className="flex items-center gap-2">
                    <span
                      className="inline-block h-2 w-2 rounded-full"
                      style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }}
                      aria-hidden
                    />
                    {entry.repo}
                  </span>
                </td>
                <td className="tabular py-2 pr-4 text-right">{count(entry.stars)}</td>
                <td className="tabular py-2 pr-4 text-right">
                  {age === null ? "—" : `${count(age)} gün`}
                </td>
                <td className="tabular py-2 text-right">
                  {age ? `${(entry.stars / Math.max(age, 1)).toFixed(1)}/g` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (value: T) => void;
  options: Array<{ value: T; label: string }>;
}) {
  return (
    <div className="border-border inline-flex rounded-md border p-0.5" role="group">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          aria-pressed={value === option.value}
          className={`rounded px-3 py-1.5 text-xs transition ${
            value === option.value
              ? "bg-surface-2 text-ink font-medium"
              : "text-ink-secondary hover:text-ink"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
