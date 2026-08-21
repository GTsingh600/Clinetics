import Link from "next/link";
import { redirect } from "next/navigation";
import SystemStatus from "@/components/SystemStatus";
import { currentUser, homeFor } from "@/lib/session";

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

const CURRENT_PHASE = 2;

export default async function Home() {
  // Signed-in users go straight to their dashboard; the marketing page is for
  // people who are not logged in.
  const user = await currentUser();
  if (user) redirect(homeFor(user.role));

  return (
    <main className="mx-auto w-full max-w-3xl px-lg py-xl">
      <h1 className="text-3xl font-bold tracking-tight">Clinetics</h1>
      <p className="mt-2 text-secondary">
        Forecast demand → optimize schedules with CP-SAT → explain and simulate via a tool-using
        agent. The LLM never makes scheduling decisions.
      </p>

      <div className="mt-lg flex gap-3">
        <Link
          href="/login"
          className="rounded-card bg-primary px-4 py-2 text-sm font-semibold text-white shadow-sm hover:opacity-90"
        >
          Sign in
        </Link>
      </div>

      <div className="mt-lg">
        <SystemStatus />
      </div>

      <h2 className="mt-xl text-sm font-semibold uppercase tracking-wide text-secondary">
        Build progress
      </h2>
      <ol className="mt-3 space-y-1 text-sm">
        {PHASES.map((p) => (
          <li key={p.n} className={p.n <= CURRENT_PHASE ? "text-on-primary-container" : "text-outline"}>
            <span className="tabular font-mono text-xs">
              [{p.n <= CURRENT_PHASE ? "x" : " "}] Phase {p.n}
            </span>{" "}
            <span className="font-medium">{p.name}</span>
            <span className="text-outline"> — {p.detail}</span>
          </li>
        ))}
      </ol>
    </main>
  );
}
