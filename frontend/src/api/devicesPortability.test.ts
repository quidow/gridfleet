import { describe, expect, it, vi } from "vitest";

import {
  commitImport,
  fetchExportBundle,
  validateImportBundle,
} from "./devicesPortability";

describe("devicesPortability", () => {
  it("requests GET /devices/export", async () => {
    const get = vi.fn().mockResolvedValue({ data: { schema_version: 1, devices: [] } });
    const client = { get } as never;
    await fetchExportBundle(client);
    expect(get).toHaveBeenCalledWith("/devices/export", expect.any(Object));
  });

  it("posts the bundle to validate", async () => {
    const post = vi.fn().mockResolvedValue({ data: { rows: [] } });
    const client = { post } as never;
    await validateImportBundle(client, { schema_version: 1 } as never);
    expect(post).toHaveBeenCalledWith("/devices/import/validate", { schema_version: 1 });
  });

  it("posts bundle + hash + mappings to commit", async () => {
    const post = vi.fn().mockResolvedValue({ data: { created: [], skipped: [], failed: [] } });
    const client = { post } as never;
    const req = { bundle: {}, bundle_hash: "sha256:x", mappings: [] };
    await commitImport(client, req as never);
    expect(post).toHaveBeenCalledWith("/devices/import", req);
  });
});
