/**
 * Typed fetch wrapper for the FastAPI backend.
 *
 * Single place that knows the base URL and error shape, so no component ever
 * hand-rolls a fetch. Works in both server and client components.
 */

const BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    // Operational data is never build-time cacheable.
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = await res.text();
    }
    throw new ApiError(`API ${res.status} on ${path}`, res.status, body);
  }

  return (await res.json()) as T;
}

export const api = {
  health: () => apiFetch<HealthResponse>("/api/v1/health"),
  readiness: () => apiFetch<ReadinessResponse>("/api/v1/health/ready"),
};

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
}

export interface ReadinessResponse {
  status: string;
  checks: Record<string, string>;
}
