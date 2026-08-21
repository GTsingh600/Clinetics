/** Unauthenticated health probe, kept separate from the session-aware client. */
import { apiFetch, ApiError } from "@/lib/api";

export { ApiError };

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
}

export const api = {
  health: () => apiFetch<HealthResponse>("/api/v1/health"),
};
