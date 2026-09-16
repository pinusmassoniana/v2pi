import { Link } from "@tanstack/react-router";
import type { Status } from "../../api/client";
import { BRAND } from "../../lib/brand";
import { cn } from "../../lib/cn";
import { SECTIONS, isTabActive } from "../nav";
import { XrayCard } from "./XrayCard";

/** Desktop navigation: every section with every tab visible, so any screen is one click away. */
export function Sidebar({ pathname, status, className }: { pathname: string; status?: Status; className?: string }) {
  return (
    <aside className={cn("sticky top-0 h-dvh flex-col gap-2 border-r border-line bg-glass p-3", className)}>
      <div className="flex items-center gap-2 px-2 pb-2 pt-1">
        <span aria-hidden className="size-6 rounded-lg bg-brand shadow-[0_0_16px_rgba(124,92,255,.5)]" />
        <span className="text-base font-extrabold tracking-tight text-t1">{BRAND}</span>
      </div>
      <nav aria-label="Primary" className="flex flex-1 flex-col gap-3 overflow-y-auto">
        {SECTIONS.map((section) => (
          <div key={section.id}>
            <p data-nav-group className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[.1em] text-t3">{section.label}</p>
            <ul className="flex flex-col gap-0.5">
              {section.tabs.map((tab) => {
                const active = isTabActive(tab.to, pathname);
                return (
                  <li key={tab.to}>
                    <Link
                      to={tab.to}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex rounded-xl px-2.5 py-1.5 text-[13px] text-t2 hover:bg-glass-2 hover:text-t1",
                        active && "bg-glass-2 text-t1 shadow-[inset_0_0_0_1px_var(--line)]",
                      )}
                    >
                      {tab.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>
      <XrayCard status={status} />
    </aside>
  );
}
