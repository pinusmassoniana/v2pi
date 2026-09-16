import { useEffect, useState } from "react";

/** How long something has run: whole seconds ("42 s"), or minutes and seconds ("0:34") for work that can take minutes. */
export function elapsedLabel(ms: number, format: "seconds" | "clock" = "seconds"): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (format === "seconds") return `${seconds} s`;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * Time since `since` (epoch ms, when the work started), counting on its own clock so only this text re-renders. Not a
 * live region: a counter read out every second would drown whatever else the screen says.
 */
export function Elapsed({ since, format = "seconds" }: { since: number; format?: "seconds" | "clock" }) {
  const [now, setNow] = useState(since);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);
  return <span aria-live="off" className="tabular-nums">{elapsedLabel(now - since, format)}</span>;
}
