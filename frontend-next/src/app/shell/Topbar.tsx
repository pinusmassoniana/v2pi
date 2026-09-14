import { Moon, Search, Sun } from "lucide-react";
import { useState } from "react";
import type { Status } from "../../api/client";
import { useTrafficIfOpen } from "../../api/traffic";
import { Button } from "../../components/ui/Button";
import { Pill } from "../../components/ui/Pill";
import { probeFor, tunnelLabel } from "../../features/home/derive";
import { applyTheme, toggleTheme, type Theme } from "../../lib/theme";
import { LogoutButton } from "./LogoutButton";
import { openPalette } from "./palette";

export function Topbar({ title, status, stale }: { title: string; status?: Status; stale: boolean }) {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.dataset.theme === "light" ? "light" : "dark"));
  const traffic = useTrafficIfOpen();
  // The same answer as Home's status block, including a failed live probe while a Home screen streams one.
  const probe = traffic.fresh ? probeFor(traffic.live, status?.active_node_id) : null;   // a frame that stopped updating decides nothing
  const tunnel = tunnelLabel(status, stale, probe);

  const flipTheme = () => {
    const next = toggleTheme(theme);
    applyTheme(next);
    setTheme(next);
  };

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-chrome px-4 backdrop-blur-md md:px-5">
      <h1 className="page-title mr-auto min-w-0 truncate text-[17px] font-bold tracking-tight text-t1">{title}</h1>
      <span aria-live="polite"><Pill tone={tunnel.tone} dot>Tunnel {tunnel.label.toLowerCase()}</Pill></span>
      <Button variant="secondary" size="sm" aria-label="Search and commands" onClick={() => openPalette()}>
        <Search size={14} aria-hidden />
        <span className="hidden text-t3 md:inline">Search…</span>
        <kbd className="hidden rounded-md bg-glass-2 px-1.5 py-0.5 text-[10.5px] text-t2 md:inline">⌘K</kbd>
      </Button>
      <Button variant="ghost" size="icon" aria-label="Toggle theme" title="Toggle theme" onClick={flipTheme}>
        {theme === "dark" ? <Sun size={16} aria-hidden /> : <Moon size={16} aria-hidden />}
      </Button>
      <LogoutButton className="hidden md:inline-flex" />
    </header>
  );
}
