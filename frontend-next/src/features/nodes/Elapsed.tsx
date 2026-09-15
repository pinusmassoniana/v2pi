import { useEffect, useState } from "react";

/** Whole seconds since `since` (epoch ms, when the work started), counting on its own clock so only this text re-renders. */
export function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(since);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);
  return <span className="tabular-nums">{Math.max(0, Math.floor((now - since) / 1000))} s</span>;
}
