import { redirect } from "next/navigation";
import { LoginForm } from "@/components/LoginForm";
import { currentUser, homeFor } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  // Already signed in? Send them where they were going. Rendering a login form
  // to a logged-in user is a small thing that makes an app feel broken.
  const user = await currentUser();
  if (user) redirect(homeFor(user.role));
  const { next } = await searchParams;

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-bold tracking-tight text-primary">Clinetics</h1>
          <p className="mt-1 text-sm text-secondary">Clinic operations, scheduled by solver.</p>
        </div>
        <LoginForm next={next} />
        <p className="mt-4 text-center text-xs text-outline">
          Synthetic demo data. No real patient information.
        </p>
      </div>
    </main>
  );
}
