import { toast } from "sonner";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { notifyError, notifyOk, notifyWarn } from "./Toaster";

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

  it("a sticky error stays until it is dismissed", () => {
    const error = vi.spyOn(toast, "error");
    notifyError(null, "not applied, and restoring the previous state reported problems: x", { sticky: true });
    expect(error).toHaveBeenCalledWith("not applied, and restoring the previous state reported problems: x", { duration: Infinity, closeButton: true });
  });
});
