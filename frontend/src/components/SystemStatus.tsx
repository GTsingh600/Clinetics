/**
 * Server component. Fetches backend health at request time.
 *
 * Deliberately a server component: it needs no interactivity, so rendering it
 * on the server means zero JavaScript ships to the browser for it. Per the
 * project conventions, client components are the exception, not the default.
 */

import { api, ApiError } from "@/lib/api";

async function probe() {
  try {
    const health = await api.health();
    return { ok: true as const, health };
  } catch (err) {
    const detail =
      err instanceof ApiError ? `HTTP ${err.status}` : "unreachable (is the API running?)";
    return { ok: false as const, detail };
  }
}

export default async function SystemStatus() {
  const result = await probe();

  return (
    <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Backend status
      </h2>
      {result.ok ? (
        <p className="mt-2 font-mono text-sm text-emerald-600 dark:text-emerald-400">
          ● {result.health.service} — {result.health.status} ({result.health.environment})
        </p>
      ) : (
        <p className="mt-2 font-mono text-sm text-amber-600 dark:text-amber-400">
          ● API {result.detail}
        </p>
      )}
    </div>
  );
}
