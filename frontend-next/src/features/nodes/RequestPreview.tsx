import type { Preview } from "../../api/client";

/** U8: the request a refresh would send — method, URL and headers. */
export function RequestPreview({ preview }: { preview: Preview }) {
  const text = [`${preview.method} ${preview.url}`, ...Object.entries(preview.headers).map(([key, value]) => `${key}: ${value}`)].join("\n");
  return (
    <pre aria-label="Request preview" className="overflow-x-auto whitespace-pre-wrap break-all rounded-xl border border-line bg-glass-2 p-3 font-mono text-[11.5px] text-t1">
      {text}
    </pre>
  );
}
