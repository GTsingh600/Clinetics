/**
 * Local-calendar date formatting.
 *
 * `Date.toISOString().slice(0, 10)` is the obvious way to get "YYYY-MM-DD" and
 * it is wrong. `toISOString` converts to UTC first, so a Date at local midnight
 * in any timezone ahead of UTC lands on the *previous* day:
 *
 *   local midnight  Fri Aug 21 2026 00:00 GMT+0530
 *   toISOString()   2026-08-20T18:30:00.000Z
 *   .slice(0, 10)   "2026-08-20"          ← a day early
 *
 * That made the week calendar render Sunday-first while claiming to be
 * Monday-based, and shifted the booking form's default and minimum dates.
 *
 * The backend deliberately stores local date + wall-clock time precisely to
 * avoid timezone drift (see docs/phase-1-database.md §2). Formatting dates
 * through UTC on the client reintroduced exactly the bug the schema was shaped
 * to prevent, so these helpers read the local calendar fields directly.
 */
export function toLocalISODate(d: Date): string {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

/** Parse "YYYY-MM-DD" as a local date, not a UTC instant. */
export function fromISODate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function todayISO(): string {
  return toLocalISODate(new Date());
}
