import { Link } from "@tanstack/react-router";
import { cn } from "../../lib/cn";
import { isTabActive, type Section } from "../nav";

/** Phone: the current section's tabs as a segmented control under the topbar. */
export function SegmentedTabs({ section, pathname, className }: { section: Section; pathname: string; className?: string }) {
  if (section.tabs.length < 2) return null;
  return (
    <nav aria-label={`${section.label} tabs`} className={cn("sticky top-14 z-20 overflow-x-auto px-4 pt-3", className)}>
      <div className="glass inline-flex gap-0.5 rounded-xl p-1">
        {section.tabs.map((tab) => {
          const on = isTabActive(tab.to, pathname);
          return (
            <Link
              key={tab.to}
              to={tab.to}
              aria-current={on ? "page" : undefined}
              className={cn("min-h-9 whitespace-nowrap rounded-lg px-3 text-xs font-semibold leading-9 text-t3", on && "bg-brand text-[#07060d]")}
            >
              {tab.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
