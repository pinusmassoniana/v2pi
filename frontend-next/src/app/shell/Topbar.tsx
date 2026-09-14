import { Moon, Sun } from "lucide-react";
import { useState } from "react";
import type { Status } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Pill } from "../../components/ui/Pill";
import { applyTheme, toggleTheme, type Theme } from "../../lib/theme";
import { LogoutButton } from "./LogoutButton";

export function Topbar({ title, status, stale }: { title: string; status?: Status; stale: boolean }) {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.dataset.theme === "light" ? "light" : "dark"));
  const tunnelOnline = status?.tunnel_online === true && !stale;
  const label = tunnelOnline ? "Tunnel online" : status?.running ? "Xray running" : "Offline";
  const tone = tunnelOnline ? "ok" : status?.running ? "warn" : "bad";

  const flipTheme = () => {
    const next = toggleTheme(theme);
    applyTheme(next);
    setTheme(next);
  };

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-chrome px-4 backdrop-blur-md md:px-5">
      <h1 className="page-title mr-auto min-w-0 truncate text-[17px] font-bold tracking-tight text-t1">{title}</h1>
      <span aria-live="polite"><Pill tone={tone} dot>{label}</Pill></span>
      <Button variant="ghost" size="icon" aria-label="Toggle theme" title="Toggle theme" onClick={flipTheme}>
        {theme === "dark" ? <Sun size={16} aria-hidden /> : <Moon size={16} aria-hidden />}
      </Button>
      <LogoutButton className="hidden md:inline-flex" />
    </header>
  );
}
