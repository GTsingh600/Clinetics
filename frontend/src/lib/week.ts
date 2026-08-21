/** Monday-based week helpers, shared by the calendar pages. */

export function weekStart(from: Date = new Date()): Date {
  const d = new Date(from);
  // getDay() is Sunday-based (0=Sun); shift so Monday starts the week.
  const shift = (d.getDay() + 6) % 7;
  d.setDate(d.getDate() - shift);
  d.setHours(0, 0, 0, 0);
  return d;
}

export function weekDays(offsetWeeks = 0): string[] {
  const start = weekStart();
  start.setDate(start.getDate() + offsetWeeks * 7);
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    return d.toISOString().slice(0, 10);
  });
}

export function weekLabel(days: string[]): string {
  const first = new Date(`${days[0]}T00:00:00`);
  const last = new Date(`${days[6]}T00:00:00`);
  const fmt = (d: Date) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return `${fmt(first)} – ${fmt(last)}`;
}
