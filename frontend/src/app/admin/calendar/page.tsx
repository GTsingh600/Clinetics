import { AppShell } from "@/components/AppShell";
import { WeekCalendar } from "@/components/WeekCalendar";
import { WeekNav } from "@/components/WeekNav";
import { Card, CardHeader } from "@/components/ui";
import { serverFetch, type Appointment, type Doctor, type Page } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";
import { weekDays, weekLabel } from "@/lib/week";

export const dynamic = "force-dynamic";

export default async function AdminCalendarPage({
  searchParams,
}: {
  searchParams: Promise<{ week?: string; doctor?: string }>;
}) {
  const user = await requireRole("admin");
  const { week, doctor } = await searchParams;
  const offset = Number.parseInt(week ?? "0", 10) || 0;
  const days = weekDays(offset);
  const cookie = await cookieHeader();

  const doctorFilter = doctor ? `&doctor_id=${doctor}` : "";
  const [page, doctors] = await Promise.all([
    serverFetch<Page<Appointment>>(
      `/api/v1/appointments?date_from=${days[0]}&date_to=${days[6]}&limit=200${doctorFilter}`,
      cookie,
    ),
    serverFetch<Doctor[]>("/api/v1/doctors", cookie),
  ]);

  return (
    <AppShell
      user={user}
      active="/admin/calendar"
      title="Clinic calendar"
      subtitle="All doctors unless filtered"
    >
      <Card>
        <CardHeader
          title={weekLabel(days)}
          subtitle={`${page?.total ?? 0} appointments${doctor ? " for this doctor" : ""}`}
          action={<WeekNav base="/admin/calendar" offset={offset} label={weekLabel(days)} />}
        />

        <div className="mb-4 flex flex-wrap gap-2">
          <a
            href={`/admin/calendar?week=${offset}`}
            className={`rounded-card border px-3 py-1 text-xs ${
              !doctor
                ? "border-primary bg-primary-container font-semibold text-on-primary-container"
                : "border-outline-variant text-secondary hover:bg-container-low"
            }`}
          >
            All doctors
          </a>
          {(doctors ?? []).slice(0, 10).map((d) => (
            <a
              key={d.id}
              href={`/admin/calendar?week=${offset}&doctor=${d.id}`}
              className={`rounded-card border px-3 py-1 text-xs ${
                doctor === String(d.id)
                  ? "border-primary bg-primary-container font-semibold text-on-primary-container"
                  : "border-outline-variant text-secondary hover:bg-container-low"
              }`}
            >
              {d.last_name}
            </a>
          ))}
        </div>

        <WeekCalendar days={days} appointments={page?.items ?? []} show="patient" />
      </Card>
    </AppShell>
  );
}
