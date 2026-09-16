import { render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { Toaster, notifyError, notifyOk, notifyWarn } from "./Toaster";
import toasterSource from "./Toaster.tsx?raw";

afterEach(() => toast.dismiss());

describe("toasts", () => {
  it("success for 8 s; a warning in amber and an error for 20 s", () => {
    const success = vi.spyOn(toast, "success");
    const warning = vi.spyOn(toast, "warning");
    const error = vi.spyOn(toast, "error");
    notifyOk("saved");
    notifyWarn("no answer yet — the gateway may still be applying; reloading");
    notifyError(new ApiError(422, "segment_iface: must not be blank"), "apply failed");
    notifyError(new Error("boom"), "apply failed");
    expect(success).toHaveBeenCalledWith("saved", { duration: 8000 });
    expect(warning).toHaveBeenCalledWith("no answer yet — the gateway may still be applying; reloading", { duration: 20000 });
    expect(error.mock.calls).toEqual([["segment_iface: must not be blank", { duration: 20000 }], ["apply failed", { duration: 20000 }]]);
  });

  it("a warning is amber on the screen, so it never reads as a failure; success and error keep the shared surface", async () => {
    render(<Toaster />);
    const NO_ANSWER = "no answer yet — the gateway may still be applying; reloading";
    notifyOk("saved");
    notifyWarn(NO_ANSWER);
    notifyError(null, "apply failed");
    for (const text of ["saved", NO_ANSWER, "apply failed"]) await screen.findByText(text);
    const surface = (text: string) => screen.getByText(text).closest("[data-sonner-toast]")!;

    expect(surface(NO_ANSWER)).toHaveAttribute("data-type", "warning");
    expect(surface(NO_ANSWER)).toHaveClass("text-warn!", "border-warn/40!");
    for (const text of ["saved", "apply failed"]) {
      // The `!` is load-bearing: sonner's own unlayered [data-sonner-toast][data-styled] rule sets a
      // background, a colour and a border, and outranks a layered utility without it — the surface was
      // being drawn by sonner, not by the panel.
      expect(surface(text)).toHaveClass("glass!", "text-t1!");
      expect(surface(text)).not.toHaveClass("text-warn!");
    }
  });

  it("the shared surface carries the important modifier on both of its utilities", () => {
    expect(toasterSource).toContain('const TOAST = "glass! text-t1!";');
    expect(toasterSource).not.toContain('"glass text-t1"');
  });

  it("a sticky error stays until it is dismissed", () => {
    const error = vi.spyOn(toast, "error");
    notifyError(null, "not applied, and restoring the previous state reported problems: x", { sticky: true });
    expect(error).toHaveBeenCalledWith("not applied, and restoring the previous state reported problems: x", { duration: Infinity, closeButton: true });
  });
});
