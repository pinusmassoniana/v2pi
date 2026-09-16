/** N19: "Showing 100 of 240 — show all" while the list is capped. */
export function RowCapFooter({ shown, total, onShowAll }: { shown: number; total: number; onShowAll: () => void }) {
  if (shown >= total) return null;
  return (
    <p className="px-1 text-xs text-t2">
      Showing {shown} of {total} —{" "}
      <button type="button" onClick={onShowAll} className="font-semibold text-t1 underline underline-offset-2">show all</button>
    </p>
  );
}
