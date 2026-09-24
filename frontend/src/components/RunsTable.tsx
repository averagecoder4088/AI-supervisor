import Link from "next/link";
import type { Run, Supervisor } from "@/api/types";
import { formatTimestamp, shortId } from "@/format";
import { OrderStatus, StatusBadge } from "./StatusBadge";

/** One row per run. `supervisors` maps id -> supervisor; a missing entry falls back to the id. */
export function RunsTable({ runs, supervisors }: { runs: Run[]; supervisors: Map<string, Supervisor> }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="px-4 py-2 font-medium">Order</th>
            <th className="px-4 py-2 font-medium">Run ID</th>
            <th className="px-4 py-2 font-medium">Supervisor</th>
            <th className="px-4 py-2 font-medium">Status</th>
            <th className="px-4 py-2 font-medium">Order status</th>
            <th className="px-4 py-2 font-medium">Created</th>
            <th className="px-4 py-2 font-medium">Completed</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {runs.map((run) => {
            const supervisor = supervisors.get(run.supervisor_id);
            return (
              <tr key={run.id} className="hover:bg-slate-50">
                <td className="px-4 py-2 font-medium">
                  <Link href={`/runs/${run.id}`} className="text-blue-700 hover:underline">
                    {run.order_id}
                  </Link>
                </td>
                <td className="px-4 py-2 font-mono text-xs text-slate-600" title={run.id}>
                  {shortId(run.id)}…
                </td>
                <td className="px-4 py-2">
                  {supervisor ? supervisor.name : <span className="font-mono text-xs">{shortId(run.supervisor_id)}…</span>}{" "}
                  <span className="text-xs text-slate-500">v{run.supervisor_version}</span>
                </td>
                <td className="px-4 py-2">
                  <StatusBadge status={run.status} />
                </td>
                <td className="px-4 py-2">
                  <OrderStatus status={run.order_status} />
                </td>
                <td className="whitespace-nowrap px-4 py-2 text-slate-600">{formatTimestamp(run.created_at)}</td>
                <td className="whitespace-nowrap px-4 py-2 text-slate-600">{formatTimestamp(run.completed_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
