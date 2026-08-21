import { Badge, EmptyState, StatusBadge } from "@/components/ui";
import { doctorName, formatDate, formatTime, patientName, type Appointment } from "@/lib/api";

/**
 * Shared appointment list, used by all three dashboards.
 *
 * `show` decides which counterparty to name: a doctor's list should show the
 * patient, a patient's list should show the doctor. Same data, different
 * question being answered.
 */
export function AppointmentList({
  appointments,
  show,
  emptyTitle,
  emptyHint,
  showDate = true,
}: {
  appointments: Appointment[];
  show: "patient" | "doctor";
  emptyTitle: string;
  emptyHint?: string;
  showDate?: boolean;
}) {
  if (appointments.length === 0) {
    return <EmptyState title={emptyTitle} hint={emptyHint} />;
  }

  return (
    <ul className="divide-y divide-outline-variant/60">
      {appointments.map((a) => (
        <li key={a.id} className="flex items-center justify-between gap-md py-3">
          <div className="min-w-0">
            <p className="flex items-center gap-2 font-medium text-on-primary-container">
              <span className="tabular">
                {showDate ? `${formatDate(a.appointment_date)} · ` : ""}
                {formatTime(a.start_time)}–{formatTime(a.end_time)}
              </span>
              {a.urgency !== "routine" ? (
                <Badge tone={a.urgency === "emergency" ? "danger" : "warning"}>{a.urgency}</Badge>
              ) : null}
            </p>
            <p className="mt-0.5 truncate text-sm text-secondary">
              {show === "patient" ? patientName(a.patient) : doctorName(a.doctor)}
              {a.specialty ? ` · ${a.specialty.name}` : ""}
              {a.is_new_patient ? " · new patient" : ""}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <span className="tabular text-xs text-outline">{a.duration_minutes}m</span>
            <StatusBadge status={a.status} />
          </div>
        </li>
      ))}
    </ul>
  );
}
