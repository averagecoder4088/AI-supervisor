import Link from "next/link";

/** Rendered inline (not via notFound()) so the message is part of the server-rendered HTML. */
export function RunNotFound() {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-10 text-center">
      <h1 className="text-lg font-semibold">Run not found</h1>
      <p className="mt-1 text-sm text-slate-500">The backend has no run with this id.</p>
      <Link href="/" className="mt-4 inline-block text-sm text-blue-700 hover:underline">
        ← Back to the dashboard
      </Link>
    </div>
  );
}
