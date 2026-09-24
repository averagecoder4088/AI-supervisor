import Link from "next/link";

export function Header() {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl items-center gap-8 px-6 py-3">
        <Link href="/" className="text-lg font-semibold tracking-tight text-slate-900">
          Order Supervisor
        </Link>
        <nav aria-label="Main" className="flex gap-5 text-sm font-medium text-slate-600">
          <Link href="/" className="hover:text-slate-900">
            Dashboard
          </Link>
          <Link href="/supervisors" className="hover:text-slate-900">
            Supervisors
          </Link>
        </nav>
      </div>
    </header>
  );
}
