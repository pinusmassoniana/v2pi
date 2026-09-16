import { Link } from "@tanstack/react-router";
import { cn } from "../../lib/cn";
import { SECTIONS, sectionForPath } from "../nav";
import { SECTION_ICONS } from "./icons";

/** Phone navigation: the five sections, no "More" drawer. */
export function BottomTabBar({ pathname, className }: { pathname: string; className?: string }) {
  const active = sectionForPath(pathname).id;
  return (
    <nav aria-label="Sections" className={cn("glass fixed inset-x-3 bottom-3 z-30 flex justify-around rounded-[20px] px-1 py-1.5", className)}>
      {SECTIONS.map((section) => {
        const Icon = SECTION_ICONS[section.id];
        const on = section.id === active;
        return (
          <Link
            key={section.id}
            to={section.tabs[0].to}
            aria-current={on ? "page" : undefined}
            className={cn("flex min-h-11 min-w-11 flex-col items-center justify-center gap-0.5 text-[10px] font-semibold text-t3", on && "text-t1")}
          >
            <Icon size={18} aria-hidden className={on ? "text-g2" : undefined} />
            {section.label}
          </Link>
        );
      })}
    </nav>
  );
}
