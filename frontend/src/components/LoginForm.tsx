"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, apiFetch, type User } from "@/lib/api";

/**
 * Client component: it owns form state and submits.
 *
 * No token handling here at all — the server sets httpOnly cookies on the
 * response and the browser stores them. There is nothing for JavaScript to
 * keep, which is exactly the property that makes the session XSS-resistant.
 */
export function LoginForm({ next }: { next?: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { user } = await apiFetch<{ user: User }>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      const destination = next ?? { admin: "/admin", doctor: "/doctor", patient: "/patient" }[user.role];
      router.push(destination);
      // Server components must re-render now that a session cookie exists.
      router.refresh();
    } catch (err) {
      // The API returns the same message for unknown email and wrong password,
      // deliberately; showing it verbatim keeps that property intact rather
      // than inventing a more specific one here.
      setError(err instanceof ApiError ? err.message : "Could not sign in. Is the API running?");
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-card border border-outline-variant bg-container-lowest p-lg shadow-sm"
    >
      <label className="block text-sm font-medium text-secondary" htmlFor="email">
        Email
      </label>
      <input
        id="email"
        type="email"
        required
        autoComplete="username"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="mt-1 w-full rounded-card border border-outline-variant bg-container-lowest px-3 py-2 text-sm"
      />

      <label className="mt-md block text-sm font-medium text-secondary" htmlFor="password">
        Password
      </label>
      <input
        id="password"
        type="password"
        required
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="mt-1 w-full rounded-card border border-outline-variant bg-container-lowest px-3 py-2 text-sm"
      />

      {error ? (
        <p role="alert" className="mt-md rounded-card bg-danger/10 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={busy}
        className="mt-lg w-full rounded-card bg-primary px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-opacity hover:opacity-90 disabled:opacity-50"
      >
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
