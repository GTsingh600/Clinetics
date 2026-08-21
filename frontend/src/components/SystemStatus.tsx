import { api, ApiError } from "@/lib/health";
import { Card } from "@/components/ui";

/**
 * Server component. Reads backend health at request time.
 *
 * No interactivity, so no JavaScript ships for it — server components are the
 * default in this project and client components the exception.
 */
async function probe() {
  try {
    return { ok: true as const, health: await api.health() };
  } catch (err) {
    const detail =
      err instanceof ApiError ? `HTTP ${err.status}` : "unreachable (is the API running?)";
    return { ok: false as const, detail };
  }
}

export default async function SystemStatus() {
  const result = await probe();
  return (
    <Card>
      <h2 className="text-xs font-semibold uppercase tracking-wide text-secondary">
        Backend status
      </h2>
      {result.ok ? (
        <p className="mt-2 font-mono text-sm text-success">
          ● {result.health.service} — {result.health.status} ({result.health.environment})
        </p>
      ) : (
        <p className="mt-2 font-mono text-sm text-warning">● API {result.detail}</p>
      )}
    </Card>
  );
}
