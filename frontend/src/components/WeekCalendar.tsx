import { Badge, EmptyState } from "@/components/ui";
import { todayISO } from "@/lib/date";
import {
  doctorName,
  formatTime,
  patientName,
  type Appointment,
} from "@/lib/api";

/**
 * Week view: days as columns, appointments stacked within each.
 *
 * A server component. A calendar that only displays does not need to be
 * interactive, and rendering it on the server keeps a potentially large list
 * out of the JavaScript bundle entirely.
 *
 * Deliberately not absolutely-positioned by time-of-day. That looks impressive
 * and reads badly: with 15-minute slots across an 11-hour day, most blocks
 * become unlabelably thin. A dense ordered list is what someone scanning their
 * week actually needs.
 */
export function WeekCalendar({
  days,
  appointments,
  show,
}: {
  days: string[];
  appointments: Appointment[];
  show: "patient" | "doctor";
}) {
  if (appointments.length === 0) {
    return <EmptyState title="Nothing scheduled this week" />;
  }

  const byDay = new Map<string, Appointment[]>(days.map((d) => [d, []]));
  for (const a of appointments) {
    byDay.get(a.appointment_date)?.push(a);
  }
  for (const list of byDay.values()) {
    list.sort((x, y) => x.start_time.localeCompare(y.start_time));
  }

  const today = todayISO();

  return (
    <div className="overflow-x-auto">
      <div className="grid min-w-[900px] grid-cols-7 gap-2">
        {days.map((day) => {
          const items = byDay.get(day) ?? [];
          const isToday = day === today;
          return (
            <div key={day} className="min-w-0">
              <div
                className={`mb-2 rounded-card px-2 py-1.5 text-center ${
                  isToday ? "bg-primary text-white" : "bg-container-low text-secondary"
                }`}
              >
                <p className="text-xs font-semibold uppercase tracking-wide">
                  {new Date(`${day}T00:00:00`).toLocaleDateString(undefined, { weekday: "short" })}
                </p>
                <p className="tabular text-sm font-bold">
                  {new Date(`${day}T00:00:00`).getDate()}
                </p>
              </div>

              <div className="space-y-1.5">
                {items.length === 0 ? (
                  <p className="py-2 text-center text-xs text-outline">—</p>
                ) : (
                  items.map((a) => (
                    <div
                      key={a.id}
                      className={`rounded-card border p-2 text-xs ${
                        a.status === "cancelled"
                          ? "border-dashed border-outline-variant bg-container text-outline"
                          : "border-outline-variant bg-container-lowest"
                      }`}
                    >
                      <p className="tabular font-semibold text-on-primary-container">
                        {formatTime(a.start_time)}–{formatTime(a.end_time)}
                      </p>
                      <p className="mt-0.5 truncate text-secondary">
                        {show === "patient" ? patientName(a.patient) : doctorName(a.doctor)}
                      </p>
                      {a.specialty ? (
                        <p className="truncate text-outline">{a.specialty.name}</p>
                      ) : null}
                      {a.status !== "scheduled" ? (
                        <div className="mt-1">
                          <Badge
                            tone={
                              a.status === "completed"
                                ? "success"
                                : a.status === "no_show"
                                  ? "warning"
                                  : "danger"
                            }
                          >
                            {a.status.replace("_", " ")}
                          </Badge>
                        </div>
                      ) : null}
                    </div>
                  ))
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
