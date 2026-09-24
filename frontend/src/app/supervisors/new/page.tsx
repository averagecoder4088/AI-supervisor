import { SupervisorForm } from "./SupervisorForm";

export default function CreateSupervisorPage() {
  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold">Create supervisor</h1>
      <p className="mb-6 text-sm text-slate-500">
        A supervisor is a reusable, versioned configuration. Creating one does not start anything: you start a run for an order next.
      </p>
      <SupervisorForm />
    </>
  );
}
