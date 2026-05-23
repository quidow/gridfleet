import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ImportResultsStep } from "./ImportResultsStep";

describe("ImportResultsStep", () => {
  it("renders counts and links created devices", () => {
    render(
      <MemoryRouter>
        <ImportResultsStep
          result={{
            created: [{ index: 0, device_id: "device-1" }],
            skipped: [{ index: 1, reason: "identity exists" }],
            failed: [{ index: 2, reason: "host not found" }],
          }}
          onReset={() => {}}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText(/1 created/i)).toBeInTheDocument();
    expect(screen.getByText(/1 skipped/i)).toBeInTheDocument();
    expect(screen.getByText(/1 failed/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /device-1/ })).toHaveAttribute(
      "href",
      "/devices/device-1",
    );
  });
});
