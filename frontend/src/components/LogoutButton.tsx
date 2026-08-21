"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiFetch } from "@/lib/api";

/**
 * Client component because it needs an onClick handler.
 *
 * `router.refresh()` after logout matters: server components cache their
 * rendered output, so without it the shell would keep showing the signed-in
 * user until a hard reload.
 */
export function LogoutButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function logout() {
    setBusy(true);
    try {
      await apiFetch("/api/v1/auth/logout", { method: "POST" });
    } catch {
      // Logout is best-effort on the client: the cookie may already be gone.
      // Navigating away is the behaviour the user asked for either way.
    }
    router.push("/login");
    router.refresh();
  }

  return (
    <button
      type="button"
      onClick={logout}
      disabled={busy}
      className="rounded-card border border-outline-variant px-3 py-1.5 text-sm font-medium text-secondary shadow-sm transition-colors hover:bg-container-low disabled:opacity-50"
    >
      {busy ? "Signing out…" : "Sign out"}
    </button>
  );
}
