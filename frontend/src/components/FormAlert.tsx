import type { FailureView } from "@/api/messages";

/** The visible failure state of a form (plain data in, so it works after a Server Action). */
export function FormAlert({ failure }: { failure: FailureView }) {
  return (
    <div role="alert" className="mb-6 rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
      <h2 className="text-base font-semibold">{failure.title}</h2>
      <p className="mt-1 whitespace-pre-line text-sm">{failure.message}</p>
      {(failure.httpStatus !== null || failure.code) && (
        <p className="mt-2 text-xs text-red-800">
          {failure.httpStatus !== null && <>HTTP {failure.httpStatus}</>}
          {failure.httpStatus !== null && failure.code && " · "}
          {failure.code && <span className="font-mono">{failure.code}</span>}
        </p>
      )}
    </div>
  );
}
