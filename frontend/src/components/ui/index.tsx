/**
 * Design-system primitives.
 *
 * All server components: none of them holds state or handles events, so none
 * needs to ship JavaScript. Interactivity is added by the few client components
 * that genuinely require it, rather than by making the whole tree client-side.
 *
 * Every colour, radius, and spacing value here comes from a token defined in
 * globals.css. See docs/design-system.md.
 */

import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
  padded = true,
}: {
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  // Data containers are flat and bordered, not elevated — the design system
  // reserves shadow for interactive elements.
  return (
    <div
      className={`rounded-card border border-outline-variant bg-container-lowest ${
        padded ? "p-md" : ""
      } ${className}`}
    >
      {children}
    </div>
  );
}

export function CardHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="mb-md flex items-start justify-between gap-md">
      <div>
        <h2 className="text-base font-bold text-primary">{title}</h2>
        {subtitle ? <p className="mt-1 text-sm text-secondary">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  unit,
  changePct,
  hint,
}: {
  label: string;
  value: number;
  unit?: string | null;
  changePct?: number | null;
  hint?: string | null;
}) {
  // `null` means "no prior period to compare against" and must not render as
  // 0%. A dashboard that shows a fabricated 0% is quietly lying.
  const hasChange = changePct !== null && changePct !== undefined;
  const positive = hasChange && changePct! > 0;

  return (
    <Card>
      <p className="text-xs font-medium uppercase tracking-wide text-secondary">{label}</p>
      <p className="tabular mt-2 text-3xl font-bold text-primary">
        {value.toLocaleString(undefined, { maximumFractionDigits: 1 })}
        {unit ? <span className="ml-1 text-lg font-semibold text-secondary">{unit}</span> : null}
      </p>
      <div className="mt-2 flex items-center gap-2 text-xs">
        {hasChange ? (
          <span className={`tabular font-semibold ${positive ? "text-danger" : "text-success"}`}>
            {positive ? "▲" : "▼"} {Math.abs(changePct!).toFixed(1)}%
          </span>
        ) : (
          <span className="text-outline" title="No prior period to compare against">
            —
          </span>
        )}
        {hint ? <span className="text-secondary">{hint}</span> : null}
      </div>
    </Card>
  );
}

type BadgeTone = "neutral" | "success" | "warning" | "danger" | "primary";

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: "bg-container text-secondary",
  primary: "bg-primary-container text-on-primary-container",
  success: "bg-success/15 text-success",
  warning: "bg-warning/20 text-[#8a5200]",
  danger: "bg-danger/12 text-danger",
};

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: BadgeTone }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  );
}

/** Appointment status → colour, in one place so the mapping cannot drift. */
export function StatusBadge({ status }: { status: string }) {
  const tone: BadgeTone =
    status === "completed"
      ? "success"
      : status === "no_show"
        ? "warning"
        : status === "cancelled"
          ? "danger"
          : "primary";
  return <Badge tone={tone}>{status.replace("_", " ")}</Badge>;
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-card border border-dashed border-outline-variant p-xl text-center">
      <p className="font-semibold text-secondary">{title}</p>
      {hint ? <p className="mt-1 text-sm text-outline">{hint}</p> : null}
    </div>
  );
}

export function ErrorState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="rounded-card border border-danger/30 bg-danger/5 p-md">
      <p className="font-semibold text-danger">{title}</p>
      {detail ? <p className="mt-1 text-sm text-secondary">{detail}</p> : null}
    </div>
  );
}

export function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return (
    // Wide tables scroll inside their own container so the page body never
    // scrolls horizontally.
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-outline-variant text-left">
            {headers.map((h) => (
              <th key={h} className="px-3 py-2 text-xs font-semibold uppercase tracking-wide text-secondary">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-outline-variant/60">{children}</tbody>
      </table>
    </div>
  );
}

/** Inline proportion bar. Uses text as well as width so it is not colour-only. */
export function Meter({ pct }: { pct: number }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const tone = clamped > 85 ? "bg-danger" : clamped > 60 ? "bg-primary" : "bg-secondary";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-24 overflow-hidden rounded-full bg-container">
        <div className={`h-full ${tone}`} style={{ width: `${clamped}%` }} />
      </div>
      <span className="tabular text-xs text-secondary">{clamped.toFixed(1)}%</span>
    </div>
  );
}
