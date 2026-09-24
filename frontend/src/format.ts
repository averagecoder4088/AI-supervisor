/** Deterministic, timezone-explicit timestamp ("2026-09-24 13:38:24 UTC"); "—" when absent. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}
