"use client";

/**
 * Slot picker and booking form.
 *
 * A client component because it is genuinely interactive: choosing a doctor and
 * date refetches slots, and booking mutates.
 *
 * Two things it deliberately does NOT do:
 *
 * 1. It does not compute availability itself. Slots come from
 *    `/appointments/slots`, which is derived by the same service the booking
 *    path uses. A client-side reimplementation would eventually disagree with
 *    the server and offer slots that then get refused.
 *
 * 2. It does not treat a 409 as an error to hide. Losing a race for a slot is
 *    normal, expected behaviour in a shared calendar; the fix is to say so
 *    plainly and refresh the grid so the taken slot disappears.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, apiFetch, formatTime, type Doctor, type Slot, type Specialty } from "@/lib/api";
import { Badge, Card, CardHeader } from "@/components/ui";
import { toLocalISODate, todayISO } from "@/lib/date";

/**
 * Default to the next weekday, not simply tomorrow.
 *
 * Most doctors work Monday-Friday, so "tomorrow" lands on a day nobody is
 * available roughly two days in seven, and the patient's first sight of the
 * booking screen is an empty grid reading "this doctor does not work on ...".
 * Opening on a day that plausibly has availability is a better starting guess;
 * the user can still pick any date.
 */
function nextWeekday(): string {
  const d = new Date();
  do {
    d.setDate(d.getDate() + 1);
  } while (d.getDay() === 0 || d.getDay() === 6);
  return toLocalISODate(d);
}

export function BookingCalendar({
  doctors,
  specialties,
  patientId,
}: {
  doctors: Doctor[];
  specialties: Specialty[];
  patientId: number | null;
}) {
  const queryClient = useQueryClient();
  const [specialtyId, setSpecialtyId] = useState<number | null>(specialties[0]?.id ?? null);
  const [doctorId, setDoctorId] = useState<number | null>(null);
  const [date, setDate] = useState(nextWeekday);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  // Only doctors holding the chosen specialty can take the appointment, and the
  // API enforces it. Filtering here keeps the UI from offering a doctor whose
  // booking would then be refused with a 422.
  const eligible = specialtyId
    ? doctors.filter((d) => d.specialties.some((s) => s.id === specialtyId))
    : doctors;
  const activeDoctorId = doctorId && eligible.some((d) => d.id === doctorId) ? doctorId : eligible[0]?.id ?? null;

  const slots = useQuery({
    queryKey: ["slots", activeDoctorId, date],
    queryFn: () =>
      apiFetch<Slot[]>(`/api/v1/appointments/slots?doctor_id=${activeDoctorId}&date=${date}`),
    enabled: activeDoctorId !== null,
  });

  const book = useMutation({
    mutationFn: (startTime: string) =>
      apiFetch("/api/v1/appointments", {
        method: "POST",
        body: JSON.stringify({
          doctor_id: activeDoctorId,
          patient_id: patientId,
          specialty_id: specialtyId,
          appointment_date: date,
          start_time: startTime,
        }),
      }),
    onSuccess: () => {
      setMessage({ tone: "ok", text: "Booked. It now appears in your upcoming appointments." });
      setSelected(null);
      // Invalidate rather than mutate the cache by hand: the server is the
      // authority on what is now free.
      queryClient.invalidateQueries({ queryKey: ["slots", activeDoctorId, date] });
    },
    onError: (err) => {
      const conflict = err instanceof ApiError && err.status === 409;
      setMessage({
        tone: "err",
        text: conflict
          ? "That slot was taken while you were choosing. The times below have been refreshed."
          : err instanceof ApiError
            ? err.message
            : "Could not book that slot.",
      });
      if (conflict) {
        queryClient.invalidateQueries({ queryKey: ["slots", activeDoctorId, date] });
      }
    },
  });

  if (patientId === null) {
    return (
      <Card>
        <p className="text-sm text-secondary">
          This account is not linked to a patient record, so it cannot book appointments.
        </p>
      </Card>
    );
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
      <Card>
        <CardHeader title="Find a time" />

        <label className="block text-sm font-medium text-secondary" htmlFor="specialty">
          Specialty
        </label>
        <select
          id="specialty"
          value={specialtyId ?? ""}
          onChange={(e) => {
            setSpecialtyId(Number(e.target.value));
            setDoctorId(null);
            setSelected(null);
          }}
          className="mt-1 w-full rounded-card border border-outline-variant bg-container-lowest px-3 py-2 text-sm"
        >
          {specialties.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} · {s.default_duration_minutes}m
            </option>
          ))}
        </select>

        <label className="mt-4 block text-sm font-medium text-secondary" htmlFor="doctor">
          Doctor
        </label>
        <select
          id="doctor"
          value={activeDoctorId ?? ""}
          onChange={(e) => {
            setDoctorId(Number(e.target.value));
            setSelected(null);
          }}
          disabled={eligible.length === 0}
          className="mt-1 w-full rounded-card border border-outline-variant bg-container-lowest px-3 py-2 text-sm disabled:opacity-50"
        >
          {eligible.map((d) => (
            <option key={d.id} value={d.id}>
              Dr. {d.first_name} {d.last_name}
            </option>
          ))}
        </select>
        {eligible.length === 0 ? (
          <p className="mt-1 text-xs text-warning">No doctor holds this specialty.</p>
        ) : null}

        <label className="mt-4 block text-sm font-medium text-secondary" htmlFor="date">
          Date
        </label>
        <input
          id="date"
          type="date"
          value={date}
          min={todayISO()}
          onChange={(e) => {
            setDate(e.target.value);
            setSelected(null);
          }}
          className="mt-1 w-full rounded-card border border-outline-variant bg-container-lowest px-3 py-2 text-sm"
        />

        {message ? (
          <p
            role="status"
            className={`mt-4 rounded-card px-3 py-2 text-sm ${
              message.tone === "ok" ? "bg-success/10 text-success" : "bg-danger/10 text-danger"
            }`}
          >
            {message.text}
          </p>
        ) : null}

        <button
          type="button"
          disabled={!selected || book.isPending}
          onClick={() => selected && book.mutate(selected)}
          className="mt-6 w-full rounded-card bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:opacity-90 disabled:opacity-40"
        >
          {book.isPending
            ? "Booking…"
            : selected
              ? `Book ${formatTime(selected)}`
              : "Select a time"}
        </button>
      </Card>

      <Card>
        <CardHeader
          title="Available times"
          subtitle="Derived from the doctor's availability, minus existing bookings"
          action={
            slots.data ? (
              <Badge tone="neutral">
                {slots.data.filter((s) => s.available).length} free
              </Badge>
            ) : null
          }
        />

        {activeDoctorId === null ? (
          <p className="py-6 text-center text-sm text-outline">Choose a specialty first.</p>
        ) : slots.isPending ? (
          <p className="py-6 text-center text-sm text-outline">Loading times…</p>
        ) : slots.isError ? (
          <p className="py-6 text-center text-sm text-danger">Could not load times.</p>
        ) : slots.data.length === 0 ? (
          <p className="py-6 text-center text-sm text-outline">
            This doctor does not work on {date}.
          </p>
        ) : (
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
            {slots.data.map((slot) => {
              const isSelected = selected === slot.start_time;
              return (
                <button
                  key={slot.start_time}
                  type="button"
                  disabled={!slot.available}
                  aria-pressed={isSelected}
                  onClick={() => setSelected(slot.start_time)}
                  className={`tabular rounded-card border px-2 py-2 text-sm transition-colors ${
                    isSelected
                      ? "border-primary bg-primary text-white"
                      : slot.available
                        ? "border-outline-variant bg-container-lowest text-on-primary-container hover:bg-primary-container"
                        : "cursor-not-allowed border-transparent bg-container text-outline line-through"
                  }`}
                  title={slot.available ? "Available" : (slot.reason ?? "Unavailable")}
                >
                  {formatTime(slot.start_time)}
                </button>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
