import { ApiError } from "@/api/client";

/**
 * One observation source that could not be loaded. Deliberately NOT role="alert": it must not be mistaken
 * for a form failure, and one failing source must not look like the whole page failed. No stack traces.
 */
export function SectionUnavailable({ what, error }: { what: string; error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const api = error instanceof ApiError ? error : null;
  return (
    <div role="note" className="rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
      <p className="font-medium">{what} unavailable</p>
      <p className="mt-1">{message}</p>
      {api && (api.status !== null || api.code) && (
        <p className="mt-1 text-xs text-amber-800">
          {api.status !== null && <>HTTP {api.status}</>}
          {api.status !== null && api.code && " · "}
          {api.code && <span className="font-mono">{api.code}</span>}
        </p>
      )}
      <p className="mt-1 text-xs text-amber-800">Use Refresh to try again. The rest of the page is unaffected.</p>
    </div>
  );
}
