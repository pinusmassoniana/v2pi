import type { ComponentProps, ReactNode } from "react";
import { Input } from "../../components/ui/Input";
import { BRAND, TAGLINE } from "../../lib/brand";

export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="grid min-h-dvh place-items-center p-4">
      <div className="glass flex w-full max-w-sm flex-col gap-4 p-6">
        <div className="flex items-center gap-3">
          <span aria-hidden className="size-9 rounded-xl bg-brand shadow-[0_0_24px_rgba(124,92,255,.5)]" />
          <div>
            <p className="text-lg font-extrabold tracking-tight text-t1">{BRAND}</p>
            <p className="text-xs text-t3">{TAGLINE}</p>
          </div>
        </div>
        {children}
      </div>
    </main>
  );
}

export function PasswordField({ shown, onToggle, ...props }: ComponentProps<"input"> & { shown: boolean; onToggle: () => void }) {
  return (
    <div className="relative">
      <Input {...props} type={shown ? "text" : "password"} className="pr-16" />
      <button
        type="button"
        onClick={onToggle}
        aria-label={shown ? "Hide password" : "Show password"}
        className="absolute inset-y-1 right-1 rounded-lg px-2 text-xs font-semibold text-t2 hover:text-t1"
      >
        {shown ? "Hide" : "Show"}
      </button>
    </div>
  );
}
