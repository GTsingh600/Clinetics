import SystemStatus from "@/components/SystemStatus";

// Live backend status must be read per request, not baked in at build time.
export const dynamic = "force-dynamic";

const PHASES = [
  { n: 0, name: "Scaffold", detail: "Repo, Docker, FastAPI + Next.js skeletons, Alembic, CI" },
  { n: 1, name: "Database", detail: "Models, constraints, EXCLUDE, trigger, synthetic data" },
  { n: 2, name: "Core app", detail: "Auth + RBAC, CRUD, calendar UI, concurrency-safe booking" },
  { n: 3, name: "Forecasting", detail: "No-show classifier, demand & duration models, eval harness" },
  { n: 4, name: "Optimizer", detail: "CP-SAT model, greedy baseline, benchmark" },
  { n: 5, name: "Agent", detail: "Tool schemas, tool-calling loop, grounded explanations" },
  { n: 6, name: "Simulation", detail: "What-if scenarios, before/after diff UI" },
  { n: 7, name: "Rigor", detail: "Load test, concurrency writeup, demo fallback, README" },
  { n: 8, name: "Deploy", detail: "Managed Postgres, backend host, Vercel frontend" },
];

const CURRENT_PHASE = 0;

export default function Home() {
  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-6 py-16">
      <h1 className="text-3xl font-bold tracking-tight">Clinetics</h1>
      <p className="mt-2 text-slate-600 dark:text-slate-400">
        Forecast demand → optimize schedules with CP-SAT → explain and simulate via a
        tool-using agent. The LLM never makes scheduling decisions.
      </p>

      <div className="mt-8">
        <SystemStatus />
      </div>

      <h2 className="mt-10 text-sm font-semibold uppercase tracking-wide text-slate-500">
        Build progress
      </h2>
      <ol className="mt-3 space-y-1">
        {PHASES.map((p) => (
          <li
            key={p.n}
            className={
              p.n <= CURRENT_PHASE
                ? "text-slate-900 dark:text-slate-100"
                : "text-slate-400 dark:text-slate-600"
            }
          >
            <span className="font-mono text-xs">
              [{p.n <= CURRENT_PHASE ? "x" : " "}] Phase {p.n}
            </span>{" "}
            <span className="font-medium">{p.name}</span>
            <span className="text-slate-400 dark:text-slate-600"> — {p.detail}</span>
          </li>
        ))}
      </ol>
    </main>
  );
}
