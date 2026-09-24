export default function Loading() {
  return (
    <div role="status" aria-live="polite" className="animate-pulse space-y-3">
      <div className="h-7 w-40 rounded bg-slate-200" />
      <div className="h-4 w-64 rounded bg-slate-200" />
      <div className="h-40 rounded-lg bg-slate-200" />
      <span className="sr-only">Loading…</span>
    </div>
  );
}
