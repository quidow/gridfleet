import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import * as api from "../../api/devicesPortability";
import { DeviceExportButton } from "./DeviceExportButton";

describe("DeviceExportButton", () => {
  it("calls downloadExportBundle on click", async () => {
    const spy = vi.spyOn(api, "downloadExportBundle").mockResolvedValue();
    render(<DeviceExportButton />);
    fireEvent.click(screen.getByRole("button", { name: /export config/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    spy.mockRestore();
  });

  it("disables the button while downloading", async () => {
    let resolve: (() => void) | null = null;
    const spy = vi.spyOn(api, "downloadExportBundle").mockImplementation(
      () => new Promise<void>((r) => { resolve = () => r(); }),
    );
    render(<DeviceExportButton />);
    const button = screen.getByRole("button", { name: /export config/i });
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    resolve?.();
    await waitFor(() => expect(button).not.toBeDisabled());
    spy.mockRestore();
  });
});
