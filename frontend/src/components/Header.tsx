import Link from "next/link";

const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/supervisors", label: "Supervisors" },
  { href: "/supervisors/new", label: "Create supervisor" },
  { href: "/runs/new", label: "Start run" },
];

export function Header() {
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-8 gap-y-2 px-6 py-3">
        <Link href="/" className="text-lg font-semibold tracking-tight text-slate-900">
          Order Supervisor
        </Link>
        <nav aria-label="Main" className="flex flex-wrap gap-5 text-sm font-medium text-slate-600">
          {LINKS.map((link) => (
            <Link key={link.href} href={link.href} className="hover:text-slate-900">
              {link.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}
