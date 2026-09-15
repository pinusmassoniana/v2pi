import { Upload } from "lucide-react";
import { useId, useState } from "react";
import { useUnsavedGuard } from "../../app/guard";
import { closeGuarded } from "../../components/confirm";
import { Button } from "../../components/ui/Button";
import { Sheet, SheetContent } from "../../components/ui/Sheet";
import { importJson, type StagedRouting } from "./rules";

export interface ImportJsonSheetProps {
  /** The ruleset on screen: an import keeps its default and strategy unless the document has its own. */
  current: StagedRouting;
  /** Stage the imported ruleset; false when the operator kept what was staged instead. */
  onImport: (next: StagedRouting) => Promise<boolean>;
  onClose: () => void;
}

/** R7: paste a ruleset — an array of rules or {rules: [...]} — to replace the staged rules. */
export function ImportJsonSheet({ current, onImport, onClose }: ImportJsonSheetProps) {
  const id = useId();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const dirty = text.trim() !== "";
  useUnsavedGuard(dirty);

  async function start() {
    const result = importJson(text, current);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    if (await onImport(result.state)) onClose();
  }

  return (
    <Sheet open dirty={dirty} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent title="Import JSON">
        <div className="flex flex-col gap-3">
          <label htmlFor={id} className="text-xs font-semibold text-t2">Paste a ruleset</label>
          <textarea
            id={id}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setError(null);
            }}
            rows={12}
            spellCheck={false}
            placeholder={'{\n  "rules": [\n    { "type": "geoip", "value": "ru", "action": "direct" }\n  ],\n  "default_action": "proxy"\n}'}
            className="w-full resize-y rounded-xl border border-line bg-glass-2 p-3 font-mono text-xs text-t1 placeholder:text-t3 focus-visible:outline-2 focus-visible:outline-g2"
          />
          <p className="text-xs text-t3">an array or {"{rules: [ ]}"} · replaces the staged rules · default and strategy change only when present</p>
          {error ? <p role="alert" className="text-xs text-bad">{error}</p> : null}
          <div className="flex justify-end gap-2">
            <Button onClick={() => void closeGuarded(dirty, onClose)}>Cancel</Button>
            <Button variant="primary" disabled={!dirty} onClick={() => void start()}>
              <Upload size={14} aria-hidden />Import
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
