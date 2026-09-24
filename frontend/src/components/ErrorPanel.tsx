import { ApiError } from "@/api/client";

/**
 * A visible failure state. Errors are caught in the page and rendered here on purpose: Next.js
 * hides the message of an error thrown from a Server Component in production, and an API failure
 * must never look like an empty page.
 */
export function ErrorPanel({ title, error }: { title: string; error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const api = error instanceof ApiError ? error : null;
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-5 text-red-900">
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-1 text-sm">{message}</p>
      {api && (
        <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs text-red-800">
          <dt className="font-medium">HTTP status</dt>
          <dd>{api.status ?? "no response"}</dd>
          <dt className="font-medium">Error code</dt>
          <dd className="font-mono">{api.code ?? "—"}</dd>
          <dt className="font-medium">Request</dt>
          <dd className="break-all font-mono">{api.url}</dd>
        </dl>
      )}
    </div>
  );
}
