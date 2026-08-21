import { AppointmentList } from "@/components/AppointmentList";
import { AppShell } from "@/components/AppShell";
import { UtilizationTrendChart } from "@/components/Charts";
import { Card, CardHeader, EmptyState, MetricCard } from "@/components/ui";
import { serverFetch, type DoctorDashboard } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function DoctorDashboardPage() {
  const user = await requireRole("doctor");
  const data = await serverFetch<DoctorDashboard>(
    "/api/v1/dashboard/doctor?days=30",
    await cookieHeader(),
  );

  // A `doctor` login with no linked Doctor row is a data gap, and the API says
  // so with a 404. Explaining that beats rendering an empty dashboard.
  if (!data) {
    return (
      <AppShell user={user} active="/doctor" title="My day">
        <EmptyState
          title="No doctor record linked to this account"
          hint="An administrator needs to connect this login to a doctor profile."
        />
      </AppShell>
    );
  }

  return (
    <AppShell
      user={user}
      active="/doctor"
      title="My day"
      subtitle={new Date().toLocaleDateString(undefined, {
        weekday: "long",
        day: "numeric",
        month: "long",
      })}
    >
      <div className="grid gap-4 sm:grid-cols-3">
        {data.metrics.map((m) => (
          <MetricCard
            key={m.label}
            label={m.label}
            value={m.value}
            unit={m.unit}
            changePct={m.change_pct}
            hint={m.hint}
          />
        ))}
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Today" subtitle="Excluding cancellations" />
          <AppointmentList
            appointments={data.today}
            show="patient"
            showDate={false}
            emptyTitle="Nothing scheduled today"
          />
        </Card>
        <Card>
          <CardHeader title="Upcoming" subtitle="Next scheduled appointments" />
          <AppointmentList
            appointments={data.upcoming}
            show="patient"
            emptyTitle="No upcoming appointments"
          />
        </Card>
      </div>

      <Card className="mt-6">
        <CardHeader title="My booked minutes" subtitle="Last 30 days" />
        <UtilizationTrendChart data={data.utilization_trend} />
      </Card>
    </AppShell>
  );
}
