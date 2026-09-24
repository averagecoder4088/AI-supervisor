"use client";

import { useFormStatus } from "react-dom";

/** Disabled while the request is in flight, so a double click cannot submit twice. */
export function SubmitButton({ label, pendingLabel }: { label: string; pendingLabel: string }) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:bg-slate-400"
    >
      {pending ? pendingLabel : label}
    </button>
  );
}
