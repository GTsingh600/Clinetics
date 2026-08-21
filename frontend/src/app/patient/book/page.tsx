import { AppShell } from "@/components/AppShell";
import { BookingCalendar } from "@/components/BookingCalendar";
import { serverFetch, type Doctor, type PatientDashboard, type Specialty } from "@/lib/api";
import { cookieHeader, requireRole } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function BookPage() {
  const user = await requireRole("patient");
  const cookie = await cookieHeader();

  // Reference data is fetched on the server and handed to the client component
  // as props, so the picker renders populated on first paint instead of
  // flashing empty selects while it fetches.
  const [specialties, doctors, dashboard] = await Promise.all([
    serverFetch<Specialty[]>("/api/v1/specialties", cookie),
    serverFetch<Doctor[]>("/api/v1/doctors", cookie),
    serverFetch<PatientDashboard>("/api/v1/dashboard/patient", cookie),
  ]);

  // The patient id comes from the caller's own dashboard, never from a URL or
  // a form field. The API refuses cross-patient bookings anyway; not putting it
  // in the client's hands means the UI cannot even try.
  const patientId = dashboard?.upcoming[0]?.patient_id ?? dashboard?.past[0]?.patient_id ?? null;

  return (
    <AppShell
      user={user}
      active="/patient/book"
      title="Book an appointment"
      subtitle="Times shown are the ones the scheduler will actually accept"
    >
      <BookingCalendar
        doctors={doctors ?? []}
        specialties={specialties ?? []}
        patientId={patientId}
      />
    </AppShell>
  );
}
