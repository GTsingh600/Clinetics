import { AppShell } from "@/components/AppShell";
import { Badge, Card, CardHeader, EmptyState, Table } from "@/components/ui";
import { serverFetch, type Doctor } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function DoctorsPage() {
  const user = await requireRole("admin");
  const doctors = await serverFetch<Doctor[]>("/api/v1/doctors", await cookieHeader());

  return (
    <AppShell
      user={user}
      active="/admin/doctors"
      title="Doctors"
      subtitle="Specialties come from the doctor_specialty junction table, so a doctor can hold several"
    >
      <Card>
        <CardHeader title={`${doctors?.length ?? 0} active doctors`} />
        {!doctors || doctors.length === 0 ? (
          <EmptyState title="No doctors" hint="Run `make seed` to generate demo data." />
        ) : (
          <Table headers={["Name", "Licence", "Specialties"]}>
            {doctors.map((d) => (
              <tr key={d.id} className="hover:bg-container-low">
                <td className="px-3 py-2 font-medium text-on-primary-container">
                  Dr. {d.first_name} {d.last_name}
                </td>
                <td className="tabular px-3 py-2 text-secondary">{d.license_number}</td>
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1.5">
                    {d.specialties.map((s) => (
                      <Badge key={s.id} tone="primary">
                        {s.name}
                      </Badge>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </AppShell>
  );
}
