import { AppShell } from "@/components/AppShell";
import { WeekCalendar } from "@/components/WeekCalendar";
import { WeekNav } from "@/components/WeekNav";
import { Card, CardHeader } from "@/components/ui";
import { serverFetch, type Appointment, type Page } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";
import { weekDays, weekLabel } from "@/lib/week";

export const dynamic = "force-dynamic";

export default async function DoctorCalendarPage({
  searchParams,
}: {
  searchParams: Promise<{ week?: string }>;
}) {
  const user = await requireRole("doctor");
  const { week } = await searchParams;
  const offset = Number.parseInt(week ?? "0", 10) || 0;
  const days = weekDays(offset);

  // No doctor_id parameter: the API scopes the query to the calling doctor.
  // Passing an id here would be an authorization hole waiting to be found.
  const page = await serverFetch<Page<Appointment>>(
    `/api/v1/appointments?date_from=${days[0]}&date_to=${days[6]}&limit=200`,
    await cookieHeader(),
  );

  return (
    <AppShell user={user} active="/doctor/calendar" title="My calendar">
      <Card>
        <CardHeader
          title={weekLabel(days)}
          subtitle={`${page?.total ?? 0} appointments this week`}
          action={<WeekNav base="/doctor/calendar" offset={offset} label={weekLabel(days)} />}
        />
        <WeekCalendar days={days} appointments={page?.items ?? []} show="patient" />
      </Card>
    </AppShell>
  );
}
