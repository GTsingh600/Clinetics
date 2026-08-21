import Link from "next/link";
import { AppointmentList } from "@/components/AppointmentList";
import { AppShell } from "@/components/AppShell";
import { Card, CardHeader, EmptyState, MetricCard } from "@/components/ui";
import { serverFetch, type PatientDashboard } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function PatientDashboardPage() {
  const user = await requireRole("patient");
  const data = await serverFetch<PatientDashboard>(
    "/api/v1/dashboard/patient",
    await cookieHeader(),
  );

  if (!data) {
    return (
      <AppShell user={user} active="/patient" title="My appointments">
        <EmptyState
          title="No patient record linked to this account"
          hint="Reception needs to connect this login to a patient profile."
        />
      </AppShell>
    );
  }

  return (
    <AppShell user={user} active="/patient" title="My appointments">
      <div className="grid gap-md sm:grid-cols-3">
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

      <div className="mt-lg grid gap-md lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Upcoming"
            action={
              <Link
                href="/patient/book"
                className="rounded-card bg-primary px-3 py-1.5 text-sm font-semibold text-white shadow-sm hover:opacity-90"
              >
                Book
              </Link>
            }
          />
          <AppointmentList
            appointments={data.upcoming}
            show="doctor"
            emptyTitle="Nothing booked"
            emptyHint="Choose a specialty and time to book an appointment."
          />
        </Card>
        <Card>
          <CardHeader title="History" subtitle="Your 20 most recent visits" />
          <AppointmentList
            appointments={data.past}
            show="doctor"
            emptyTitle="No past appointments"
          />
        </Card>
      </div>
    </AppShell>
  );
}
