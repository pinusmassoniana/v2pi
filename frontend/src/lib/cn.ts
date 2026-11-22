import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The type ramp in styles/index.css: without this, `text-caption` reads as a colour and loses to `text-t3`.
const twMerge = extendTailwindMerge({
  extend: { theme: { text: ["2xs", "caption", "body", "title", "heading", "kpi", "display"] } },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
