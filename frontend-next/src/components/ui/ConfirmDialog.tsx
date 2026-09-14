import { Dialog as Primitive } from "radix-ui";
import { settleConfirm, useConfirmRequest } from "../confirm";
import { Button } from "./Button";

/** Mounted once by the shell. Sits above sheets and dialogs (z-60). */
export function ConfirmDialog() {
  const request = useConfirmRequest();
  return (
    <Primitive.Root open={request !== null} onOpenChange={(open) => { if (!open) settleConfirm(false); }}>
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-[60] bg-[rgba(4,3,10,.6)] backdrop-blur-sm" />
        <Primitive.Content
          aria-describedby={undefined}
          className="glass fixed left-1/2 top-1/2 z-[60] w-[min(28rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 p-5"
        >
          <Primitive.Title className="text-base font-bold text-t1">Confirm</Primitive.Title>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-t2">{request?.message}</p>
          <div className="mt-5 flex justify-end gap-2">
            <Button onClick={() => settleConfirm(false)}>Cancel</Button>
            <Button variant={request?.danger === false ? "primary" : "danger"} onClick={() => settleConfirm(true)}>
              {request?.confirmLabel ?? "Confirm"}
            </Button>
          </div>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
