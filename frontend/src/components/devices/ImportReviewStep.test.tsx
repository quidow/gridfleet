import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ImportReviewStep } from "./ImportReviewStep";
import type { ImportPreview } from "../../api/devicesPortability";

const preview: ImportPreview = {
  schema_version: 1,
  exported_at: "2026-05-23T00:00:00Z",
  source_instance: "alpha",
  bundle_hash: "sha256:x",
  available_hosts: [{ id: "host-1", hostname: "lab-04" }],
  rows: [
    {
      index: 0,
      device: { name: "Pixel" } as never,
      status: "valid_new",
      host_suggestion: { id: "host-1", hostname: "lab-04" },
      issues: [],
    },
    {
      index: 1,
      device: { name: "Dup" } as never,
      status: "conflict_skip",
      host_suggestion: null,
      issues: [],
    },
  ],
};

describe("ImportReviewStep", () => {
  it("renders rows with status badges", () => {
    render(
      <ImportReviewStep
        preview={preview}
        mappings={{ 0: { target_host_id: "host-1", included: true } }}
        onSetMapping={() => {}}
        onToggleIncluded={() => {}}
        onCommit={() => {}}
      />,
    );
    expect(screen.getByText("Pixel")).toBeInTheDocument();
    expect(screen.getByText(/valid/i)).toBeInTheDocument();
    expect(screen.getByText(/conflict/i)).toBeInTheDocument();
  });

  it("enables commit when every included row has a host", () => {
    const onCommit = vi.fn();
    render(
      <ImportReviewStep
        preview={preview}
        mappings={{ 0: { target_host_id: "host-1", included: true } }}
        onSetMapping={() => {}}
        onToggleIncluded={() => {}}
        onCommit={onCommit}
      />,
    );
    const commit = screen.getByRole("button", { name: /commit/i });
    expect(commit).not.toBeDisabled();
    fireEvent.click(commit);
    expect(onCommit).toHaveBeenCalled();
  });

  it("disables commit when an included row lacks a host", () => {
    render(
      <ImportReviewStep
        preview={preview}
        mappings={{ 0: { target_host_id: "", included: true } }}
        onSetMapping={() => {}}
        onToggleIncluded={() => {}}
        onCommit={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /commit/i })).toBeDisabled();
  });
});
