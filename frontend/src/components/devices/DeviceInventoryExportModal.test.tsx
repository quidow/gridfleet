import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import * as api from "../../api/devicesInventory";
import { DeviceInventoryExportModal } from "./DeviceInventoryExportModal";

beforeEach(() => {
  localStorage.clear();
});

describe("DeviceInventoryExportModal", () => {
  it("downloads inventory with chosen format and columns", async () => {
    const spy = vi.spyOn(api, "downloadInventory").mockResolvedValue();
    render(<DeviceInventoryExportModal isOpen onClose={() => {}} filters={{}} />);
    fireEvent.click(screen.getByLabelText(/csv/i));
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ format: "csv" })),
    );
    spy.mockRestore();
  });

  it("persists column selection to localStorage", async () => {
    const spy = vi.spyOn(api, "downloadInventory").mockResolvedValue();
    render(<DeviceInventoryExportModal isOpen onClose={() => {}} filters={{}} />);
    const checkbox = screen.getByLabelText("tags");
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    await waitFor(() => {
      const stored = localStorage.getItem("gridfleet:inventory-export-columns");
      expect(stored).toBeTruthy();
      expect(stored).toContain("tags");
    });
    spy.mockRestore();
  });

  it("does not render when isOpen=false", () => {
    render(<DeviceInventoryExportModal isOpen={false} onClose={() => {}} filters={{}} />);
    expect(screen.queryByText(/export inventory/i)).toBeNull();
  });

  it("forwards filters prop into downloadInventory", async () => {
    const spy = vi.spyOn(api, "downloadInventory").mockResolvedValue();
    render(
      <DeviceInventoryExportModal
        isOpen
        onClose={() => {}}
        filters={{ pack_id: "appium-uiautomator2", search: "pixel" }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /download/i }));
    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith(
        expect.objectContaining({
          filters: { pack_id: "appium-uiautomator2", search: "pixel" },
        }),
      ),
    );
    spy.mockRestore();
  });
});
