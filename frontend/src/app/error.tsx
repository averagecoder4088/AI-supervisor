"use client";

// Fallback for unexpected rendering errors. Backend/API failures are handled in each page and
// shown with their real message; this only catches bugs the pages did not anticipate.
export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-5 text-red-900">
      <h2 className="text-base font-semibold">Something went wrong</h2>
      <p className="mt-1 text-sm">The page failed to render{error.digest ? ` (digest ${error.digest})` : ""}.</p>
      <button
        type="button"
        onClick={reset}
        className="mt-3 rounded border border-red-300 bg-white px-3 py-1 text-sm hover:bg-red-100"
      >
        Try again
      </button>
    </div>
  );
}
