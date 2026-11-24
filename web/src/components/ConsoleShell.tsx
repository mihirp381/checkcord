"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth } from "../lib/auth";
import { checkcordVersion } from "../lib/version";
import { Button, ErrorState, Loading } from "./ui";

export const SECTIONS = [
  { href: "/console", label: "Overview", admin: true },
  { href: "/console/connections", label: "Connections", admin: true },
  { href: "/console/review", label: "Review", admin: true },
  { href: "/console/replacements", label: "Replacements", admin: true },
  { href: "/console/records", label: "Records", admin: true },
  { href: "/console/people", label: "People", admin: true },
  { href: "/console/keys", label: "API keys", admin: true },
  { href: "/console/audit", label: "Audit", admin: true },
  { href: "/console/audiences", label: "Audiences", admin: true },
  { href: "/console/pipeline", label: "Pipeline", admin: true },
  { href: "/console/graph", label: "Graph", admin: false },
  { href: "/console/account", label: "Your account", admin: false },
];

const isCurrent = (pathname: string, href: string) =>
  href === "/console" ? pathname === href : pathname === href || pathname.startsWith(`${href}/`);

export function ConsoleShell({ children }: { children: React.ReactNode }) {
  const { state, signOut, reload } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (state.status !== "signed-out") return;
    const next = `${window.location.pathname}${window.location.search}`;
    const ended = state.ended ? "&ended=1" : "";
    router.replace(`/sign-in?next=${encodeURIComponent(next)}${ended}`);
  }, [state, router]);

  if (state.status === "loading" || state.status === "signed-out") {
    return (
      <main className="mx-auto w-full max-w-5xl p-6">
        <Loading label={state.status === "loading" ? "Checking your sign-in" : "Going to sign-in"} />
      </main>
    );
  }
  if (state.status === "error") {
    return (
      <main className="mx-auto w-full max-w-5xl p-6">
        <ErrorState message={state.message} onRetry={() => void reload()} />
      </main>
    );
  }

  const { me } = state;
  const sections = SECTIONS.filter((s) => me.is_admin || !s.admin);
  const who = me.email ?? me.key_name ?? "API key";

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-panel focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-panel px-4 py-2.5">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            small
            className="md:hidden"
            aria-expanded={menuOpen}
            aria-controls="console-nav"
            onClick={() => setMenuOpen((open) => !open)}
          >
            Menu
          </Button>
          <Link href={me.is_admin ? "/console" : "/console/graph"} className="font-semibold tracking-tight">
            Checkcord <span className="font-normal text-muted">console</span>
          </Link>
        </div>
        <div className="flex min-w-0 items-center gap-3 text-sm">
          <span className="min-w-0 truncate" title={who}>
            {who}
            <span className="text-muted">
              {" · "}
              {me.is_admin ? "admin" : "member"}
              {me.auth === "key" ? ", API key" : ""}
            </span>
          </span>
          <Button small onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>
      <div className="flex flex-1 flex-col md:flex-row">
        <nav
          id="console-nav"
          aria-label="Console sections"
          className={`${menuOpen ? "block" : "hidden"} border-b border-line bg-panel md:block md:w-52 md:shrink-0 md:border-b-0 md:border-r`}
        >
          <ul className="flex flex-col gap-0.5 p-2">
            {sections.map((s) => {
              const current = isCurrent(pathname, s.href);
              return (
                <li key={s.href}>
                  <Link
                    href={s.href}
                    aria-current={current ? "page" : undefined}
                    onClick={() => setMenuOpen(false)}
                    className={`block rounded-md px-3 py-1.5 text-sm ${current ? "bg-subtle font-semibold text-accent" : "hover:bg-subtle"}`}
                  >
                    {s.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 p-4 md:p-6">
          {children}
        </main>
      </div>
      <footer className="border-t border-line bg-panel px-4 py-2 text-xs text-muted">Checkcord {checkcordVersion()}</footer>
    </div>
  );
}

export function AdminOnly({ children, home = false }: { children: React.ReactNode; home?: boolean }) {
  const { state } = useAuth();
  const router = useRouter();
  const denied = state.status === "signed-in" && !state.me.is_admin;
  useEffect(() => {
    if (denied && home) router.replace("/console/graph");
  }, [denied, home, router]);
  if (denied && home) return <Loading label="Opening the graph" />;
  if (denied) {
    return (
      <div className="max-w-xl">
        <h1 className="text-xl font-semibold">This needs admin rights</h1>
        <p className="mt-2 text-sm text-muted">
          Your account can open the <Link className="underline" href="/console/graph">graph</Link> and{" "}
          <Link className="underline" href="/console/account">your own details</Link>. Ask an admin if
          you need more.
        </p>
      </div>
    );
  }
  return <>{children}</>;
}
