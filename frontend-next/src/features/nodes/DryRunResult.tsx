import type { PreviewNodes } from "../../api/client";

/** U9: what a refresh would parse — count and format, the truncation note, and the nodes it returned. */
export function DryRunResult({ result }: { result: PreviewNodes }) {
  return (
    <section aria-label="Dry-run result" className="flex flex-col gap-2 rounded-xl border border-line bg-glass p-3">
      <p className="text-sm font-semibold text-t1">Dry-run: {result.count} node(s) · <span className="font-normal text-t2">{result.format}</span></p>
      {result.truncated ? <p className="text-xs text-warn">Showing first {result.returned_count} of {result.count} nodes.</p> : null}
      {result.count === 0 ? (
        <p className="text-xs text-t2">No nodes parsed — check the URL, token, or format.</p>
      ) : (
        <div className="max-h-56 overflow-auto">
          <table className="w-full text-left text-[11.5px]">
            <thead className="text-[9.5px] uppercase tracking-[.07em] text-t3">
              <tr>
                <th scope="col" className="py-1 pr-2 font-semibold">name</th>
                <th scope="col" className="py-1 pr-2 font-semibold">address</th>
                <th scope="col" className="py-1 pr-2 font-semibold">port</th>
                <th scope="col" className="py-1 pr-2 font-semibold">transport</th>
                <th scope="col" className="py-1 font-semibold">security</th>
              </tr>
            </thead>
            <tbody>
              {result.nodes.map((node, index) => (
                <tr key={`${node.address}:${node.port}#${index}`} className="border-t border-line">
                  <td className="max-w-40 truncate py-1 pr-2 text-t1">{node.name}</td>
                  <td className="py-1 pr-2 font-mono text-t2">{node.address}</td>
                  <td className="py-1 pr-2 font-mono text-t2">{node.port}</td>
                  <td className="py-1 pr-2 text-t2">{node.transport}</td>
                  <td className="py-1 text-t2">{node.security}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
