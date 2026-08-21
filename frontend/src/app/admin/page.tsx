import { AppShell } from "@/components/AppShell";
import { DemandChart, UtilizationTrendChart } from "@/components/Charts";
import { Card, CardHeader, EmptyState, MetricCard, Meter, Table } from "@/components/ui";
import { serverFetch, type AdminDashboard } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";

// Live operational data: must be read per request, never baked in at build time.
export const dynamic = "force-dynamic";

export default async function AdminDashboardPage() {
  const user = await requireRole("admin");
  const data = await serverFetch<AdminDashboard>(
    "/api/v1/dashboard/admin?days=30",
    await cookieHeader(),
  );

  if (!data) {
    return (
      <AppShell user={user} active="/admin" title="Overview">
        <EmptyState title="Dashboard unavailable" hint="The API refused the request." />
      </AppShell>
    );
  }

  // Busiest first: the admin's question is "where is capacity tight?", so the
  // default sort should answer it without anyone clicking a column header.
  const doctors = [...data.utilization_by_doctor].sort(
    (a, b) => b.utilization_pct - a.utilization_pct,
  );

  return (
    <AppShell
      user={user}
      active="/admin"
      title="Clinic overview"
      subtitle="Last 30 days, compared with the preceding 30"
    >
      <div className="grid gap-md sm:grid-cols-2 lg:grid-cols-4">
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
            title="Demand by hour"
            subtitle="Appointments per hour of day, excluding cancellations"
          />
          <DemandChart data={data.demand_by_hour} />
        </Card>
        <Card>
          <CardHeader title="Booked minutes" subtitle="Daily total across all doctors" />
          <UtilizationTrendChart data={data.utilization_trend} />
        </Card>
      </div>

      <Card className="mt-lg">
        <CardHeader
          title="Doctor utilisation"
          subtitle="Booked minutes against scheduled availability. Maintained by a database trigger."
        />
        {doctors.length === 0 ? (
          <EmptyState title="No doctors yet" hint="Run `make seed` to generate demo data." />
        ) : (
          <Table headers={["Doctor", "Utilisation", "Booked", "Scheduled", "Completed", "No-show", "Cancelled"]}>
            {doctors.map((d) => (
              <tr key={d.doctor_id} className="hover:bg-container-low">
                <td className="px-3 py-2 font-medium text-on-primary-container">{d.doctor_name}</td>
                <td className="px-3 py-2">
                  <Meter pct={d.utilization_pct} />
                </td>
                <td className="tabular px-3 py-2 text-secondary">
                  {(d.booked_minutes / 60).toFixed(1)}h
                </td>
                <td className="tabular px-3 py-2 text-secondary">{d.scheduled_count}</td>
                <td className="tabular px-3 py-2 text-secondary">{d.completed_count}</td>
                <td className="tabular px-3 py-2 text-warning">{d.no_show_count}</td>
                <td className="tabular px-3 py-2 text-secondary">{d.cancelled_count}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </AppShell>
  );
}
