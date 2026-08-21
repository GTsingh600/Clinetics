import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { serverFetch, type User, type UserRole } from "@/lib/api";

/**
 * Server-side session helpers.
 *
 * The session is whatever `/auth/me` says it is. The frontend deliberately does
 * not decode the JWT itself: the cookie is httpOnly (so the token is not
 * readable here anyway), and more importantly the *server* is the authority on
 * identity. A client that decoded the token would be trusting a value the user
 * controls, and would miss a mid-session demotion or deactivation, both of
 * which the backend applies immediately.
 *
 * The cost is one API call per protected render, which is a cheap price for
 * never showing a UI the API will refuse to serve.
 */

export async function currentUser(): Promise<User | null> {
  const cookieHeader = (await cookies()).toString();
  if (!cookieHeader) return null;
  return serverFetch<User>("/api/v1/auth/me", cookieHeader);
}

export async function cookieHeader(): Promise<string> {
  return (await cookies()).toString();
}

/** Home route for a role. Used after login and to bounce misrouted users. */
export function homeFor(role: UserRole): string {
  return { admin: "/admin", doctor: "/doctor", patient: "/patient" }[role];
}

/**
 * Gate a page on a role.
 *
 * This is UX, not security: the API enforces authorization independently, and
 * would refuse the data even if someone reached the page. Its job is to avoid
 * rendering a dashboard that would then fail to load, and to send people
 * somewhere useful instead of showing an empty shell.
 */
export async function requireRole(role: UserRole): Promise<User> {
  const user = await currentUser();
  if (!user) redirect(`/login?next=${homeFor(role)}`);
  if (user.role !== role) redirect(homeFor(user.role));
  return user;
}
