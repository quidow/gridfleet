import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ImportUploadStep } from "./ImportUploadStep";

describe("ImportUploadStep", () => {
  it("parses uploaded JSON and calls onBundle", async () => {
    const onBundle = vi.fn();
    render(<ImportUploadStep onBundle={onBundle} />);
    const file = new File(
      ['{"schema_version":1,"exported_at":"2026-05-23T00:00:00Z","devices":[]}'],
      "bundle.json",
      { type: "application/json" },
    );
    const input = screen.getByLabelText(/bundle/i) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(onBundle).toHaveBeenCalledWith(expect.objectContaining({ schema_version: 1 })),
    );
  });

  it("shows error when schema_version is not 1", async () => {
    render(<ImportUploadStep onBundle={() => {}} />);
    const file = new File(
      ['{"schema_version":99,"exported_at":"2026-05-23T00:00:00Z","devices":[]}'],
      "bundle.json",
    );
    const input = screen.getByLabelText(/bundle/i) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(screen.getByText(/unsupported schema_version/i)).toBeInTheDocument(),
    );
  });

  it("shows error on invalid JSON", async () => {
    render(<ImportUploadStep onBundle={() => {}} />);
    const file = new File(["not json"], "bundle.json");
    const input = screen.getByLabelText(/bundle/i) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() =>
      expect(screen.getByText(/could not parse json/i)).toBeInTheDocument(),
    );
  });
});
