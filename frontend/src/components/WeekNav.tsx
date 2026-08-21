import Link from "next/link";

/** Previous / this / next week links. Plain links, so no JavaScript needed. */
export function WeekNav({ base, offset, label }: { base: string; offset: number; label: string }) {
  const link = "rounded-card border border-outline-variant px-3 py-1.5 text-sm text-secondary hover:bg-container-low";
  return (
    <div className="flex items-center gap-2">
      <Link href={`${base}?week=${offset - 1}`} className={link} aria-label="Previous week">
        ←
      </Link>
      <span className="tabular px-2 text-sm font-medium text-secondary">{label}</span>
      <Link href={`${base}?week=${offset + 1}`} className={link} aria-label="Next week">
        →
      </Link>
      {offset !== 0 ? (
        <Link href={base} className={`${link} font-semibold`}>
          This week
        </Link>
      ) : null}
    </div>
  );
}
