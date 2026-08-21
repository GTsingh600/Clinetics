/**
 * Typed fetch wrapper for the FastAPI backend.
 *
 * Session tokens live in httpOnly cookies, which has one consequence that
 * shapes this whole file: **the browser attaches them automatically, but the
 * Next.js server does not.**
 *
 * A server component runs on the Node process, not in the browser, so its
 * `fetch` has no cookie jar. To call the API as the logged-in user it must read
 * the incoming request's cookies and forward them explicitly — that is what
 * `serverFetch` does. Client components use `apiFetch` with
 * `credentials: "include"`, where the browser handles it.
 *
 * Getting this wrong is the classic App Router auth bug: everything works in
 * dev while you happen to be hitting client-rendered routes, then every server
 * component 401s.
 */

const BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function toError(res: Response): Promise<ApiError> {
  let detail: string | undefined;
  try {
    const body = await res.json();
    // FastAPI puts a string in `detail` for HTTPException and an array of
    // field errors for validation failures.
    detail =
      typeof body?.detail === "string"
        ? body.detail
        : Array.isArray(body?.detail)
          ? body.detail.map((d: { msg: string }) => d.msg).join("; ")
          : undefined;
  } catch {
    detail = undefined;
  }
  return new ApiError(detail ?? `Request failed (${res.status})`, res.status, detail);
}

/** Browser-side fetch. `credentials: "include"` sends the session cookie. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/**
 * Server-component fetch. Forwards the caller's cookies to the API.
 *
 * Returns `null` on 401/403 rather than throwing, so a page can render a
 * "please sign in" state instead of crashing the whole route. Other failures
 * still throw, because they are genuine errors rather than an expected state.
 */
export async function serverFetch<T>(path: string, cookieHeader: string): Promise<T | null> {
  const res = await fetch(`${BASE_URL}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", cookie: cookieHeader },
  });
  if (res.status === 401 || res.status === 403) return null;
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Response types — mirror the Pydantic schemas in backend/app/schemas/.
// ---------------------------------------------------------------------------
export type UserRole = "patient" | "doctor" | "admin";
export type AppointmentStatus = "scheduled" | "completed" | "cancelled" | "no_show";

export interface User {
  id: number;
  email: string;
  role: UserRole;
  full_name: string | null;
  is_active: boolean;
}

export interface Specialty {
  id: number;
  name: string;
  slug: string;
  default_duration_minutes: number;
}

export interface DoctorSummary {
  id: number;
  first_name: string;
  last_name: string;
}

export interface Doctor extends DoctorSummary {
  license_number: string;
  is_active: boolean;
  specialties: Specialty[];
}

export interface PatientSummary {
  id: number;
  first_name: string;
  last_name: string;
}

export interface Appointment {
  id: number;
  doctor_id: number;
  patient_id: number;
  specialty_id: number;
  room_id: number | null;
  appointment_date: string;
  start_time: string;
  end_time: string;
  duration_minutes: number;
  status: AppointmentStatus;
  urgency: "routine" | "urgent" | "emergency";
  is_new_patient: boolean;
  booked_at: string;
  doctor?: DoctorSummary | null;
  patient?: PatientSummary | null;
  specialty?: Specialty | null;
}

export interface Slot {
  start_time: string;
  end_time: string;
  available: boolean;
  reason: string | null;
}

export interface MetricCardData {
  label: string;
  value: number;
  unit: string | null;
  change_pct: number | null;
  hint: string | null;
}

export interface DoctorUtilizationRow {
  doctor_id: number;
  doctor_name: string;
  booked_minutes: number;
  available_minutes: number;
  utilization_pct: number;
  scheduled_count: number;
  completed_count: number;
  cancelled_count: number;
  no_show_count: number;
}

export interface UtilizationPoint {
  date: string;
  booked_minutes: number;
  available_minutes: number;
  utilization_pct: number;
}

export interface DemandPoint {
  hour: number;
  count: number;
}

export interface AdminDashboard {
  metrics: MetricCardData[];
  utilization_by_doctor: DoctorUtilizationRow[];
  demand_by_hour: DemandPoint[];
  utilization_trend: UtilizationPoint[];
}

export interface DoctorDashboard {
  metrics: MetricCardData[];
  today: Appointment[];
  upcoming: Appointment[];
  utilization_trend: UtilizationPoint[];
}

export interface PatientDashboard {
  metrics: MetricCardData[];
  upcoming: Appointment[];
  past: Appointment[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Formatting helpers, shared so times render identically everywhere.
// ---------------------------------------------------------------------------
export function formatTime(t: string): string {
  return t.slice(0, 5); // "09:30:00" -> "09:30"
}

export function formatDate(d: string): string {
  return new Date(`${d}T00:00:00`).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

export function doctorName(d?: DoctorSummary | null): string {
  return d ? `Dr. ${d.first_name} ${d.last_name}` : "—";
}

export function patientName(p?: PatientSummary | null): string {
  return p ? `${p.first_name} ${p.last_name}` : "—";
}
