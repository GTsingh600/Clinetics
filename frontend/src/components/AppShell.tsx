import Link from "next/link";
import type { ReactNode } from "react";
import type { User } from "@/lib/api";
import { LogoutButton } from "@/components/LogoutButton";

/**
 * Application chrome: 260px side navigation plus a header, per the design
 * system. A server component — the only interactive part is the logout button,
 * which is its own small client component, so navigation ships no JavaScript.
 */

const NAV: Record<string, { href: string; label: string; icon: string }[]> = {
  admin: [
    { href: "/admin", label: "Overview", icon: "▤" },
    { href: "/admin/calendar", label: "Calendar", icon: "▦" },
    { href: "/admin/doctors", label: "Doctors", icon: "◉" },
  ],
  doctor: [
    { href: "/doctor", label: "My day", icon: "▤" },
    { href: "/doctor/calendar", label: "My calendar", icon: "▦" },
  ],
  patient: [
    { href: "/patient", label: "My appointments", icon: "▤" },
    { href: "/patient/book", label: "Book", icon: "＋" },
  ],
};

export function AppShell({
  user,
  active,
  title,
  subtitle,
  children,
}: {
  user: User;
  active: string;
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  const items = NAV[user.role] ?? [];
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-[260px] shrink-0 border-r border-outline-variant bg-container-lowest md:block">
        <div className="border-b border-outline-variant px-4 py-6">
          <Link href="/" className="text-lg font-bold tracking-tight text-primary">
            Clinetics
          </Link>
          <p className="mt-1 text-xs uppercase tracking-wide text-secondary">{user.role}</p>
        </div>
        <nav className="p-2">
          {items.map((item) => {
            const isActive = item.href === active;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isActive ? "page" : undefined}
                className={`mb-1 flex items-center gap-3 rounded-card px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-primary-container font-bold text-on-primary-container"
                    : "text-secondary hover:bg-container-low"
                }`}
              >
                <span aria-hidden className="text-base">
                  {item.icon}
                </span>
                {item.label}
              </Link>
            );
          })}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-outline-variant bg-container-lowest px-6 py-4">
          <div className="min-w-0">
            <h1 className="truncate text-xl font-bold text-primary">{title}</h1>
            {subtitle ? <p className="mt-0.5 text-sm text-secondary">{subtitle}</p> : null}
          </div>
          <div className="flex shrink-0 items-center gap-4">
            <span className="hidden text-sm text-secondary sm:inline">
              {user.full_name ?? user.email}
            </span>
            <LogoutButton />
          </div>
        </header>
        <main className="flex-1 p-6">{children}</main>
      </div>
    </div>
  );
}
