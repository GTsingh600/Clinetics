"use client";

/**
 * Recharts wrappers.
 *
 * Client components by necessity: Recharts measures the DOM to size itself, so
 * it cannot render on the server. They are kept small and leaf-level so only
 * the chart ships JavaScript, not the page around it.
 *
 * Axis margins are zero on the left, not negative. A negative left margin pulls
 * the plot area over the axis and silently clips the tick labels: "140" renders
 * as "40" and "1500" as "00", which is worse than no axis at all because it
 * looks like data.
 *
 * Colours come from the design system's clinical palette — navy, teal, slate.
 * The brief explicitly rules out vibrant "neon" chart colours, and there is a
 * practical reason beyond taste: saturated hues on a light clinical surface
 * imply alarm, and a chart that looks urgent when nothing is wrong trains
 * people to ignore it.
 */

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { DemandPoint, UtilizationPoint } from "@/lib/api";

const NAVY = "#0d4a76";
const TEAL = "#2a7f8f";
const SLATE = "#535f70";
const GRID = "#c4c6cf";

const AXIS = {
  stroke: SLATE,
  fontSize: 11,
  tickLine: false,
} as const;

const TOOLTIP_STYLE = {
  borderRadius: 8,
  border: `1px solid ${GRID}`,
  fontSize: 12,
  boxShadow: "0 1px 2px rgba(0,0,0,0.06)",
} as const;

export function DemandChart({ data }: { data: DemandPoint[] }) {
  if (data.length === 0) {
    return <p className="py-6 text-center text-sm text-outline">No appointments in this window.</p>;
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="hour"
          {...AXIS}
          tickFormatter={(h: number) => `${String(h).padStart(2, "0")}`}
        />
        <YAxis {...AXIS} width={48} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(h) => `${String(h).padStart(2, "0")}:00`}
          formatter={(v) => [Number(v ?? 0), "appointments"]}
        />
        <Bar dataKey="count" fill={NAVY} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function UtilizationTrendChart({ data }: { data: UtilizationPoint[] }) {
  if (data.length === 0) {
    return <p className="py-6 text-center text-sm text-outline">No utilisation data yet.</p>;
  }
  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="bookedFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={TEAL} stopOpacity={0.35} />
            <stop offset="100%" stopColor={TEAL} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID} strokeDasharray="2 4" vertical={false} />
        <XAxis
          dataKey="date"
          {...AXIS}
          // Dates only, no year: the window is 30 days and the year is noise.
          tickFormatter={(d: string) => d.slice(5)}
          minTickGap={24}
        />
        <YAxis
          {...AXIS}
          width={52}
          // Minutes are stored, hours are readable. 1,500 minutes means nothing
          // at a glance; 25h does.
          tickFormatter={(v: number) => `${Math.round(v / 60)}h`}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v) => [`${(Number(v ?? 0) / 60).toFixed(1)} h`, "booked"]}
        />
        <Area
          type="monotone"
          dataKey="booked_minutes"
          stroke={TEAL}
          strokeWidth={2}
          fill="url(#bookedFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
