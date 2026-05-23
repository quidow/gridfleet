import { describe, expect, it } from "vitest";

import { deviceImportReducer, initialDeviceImportState } from "./useDeviceImport";

describe("deviceImportReducer", () => {
  it("transitions from idle to validating to reviewing", () => {
    let state = initialDeviceImportState();
    state = deviceImportReducer(state, { type: "UPLOAD_START", bundle: { schema_version: 1 } as never });
    expect(state.status).toBe("validating");

    state = deviceImportReducer(state, {
      type: "UPLOAD_OK",
      preview: {
        rows: [],
        available_hosts: [],
        bundle_hash: "sha256:x",
        schema_version: 1,
        exported_at: "",
      } as never,
    });
    expect(state.status).toBe("reviewing");
    expect(state.preview?.bundle_hash).toBe("sha256:x");
  });

  it("sets mapping per row", () => {
    let state = initialDeviceImportState();
    state = deviceImportReducer(state, { type: "UPLOAD_START", bundle: { schema_version: 1 } as never });
    state = deviceImportReducer(state, {
      type: "UPLOAD_OK",
      preview: {
        rows: [{ index: 0, device: {}, status: "valid_new", host_suggestion: null, issues: [] }],
        available_hosts: [],
        bundle_hash: "sha256:x",
        schema_version: 1,
        exported_at: "",
      } as never,
    });
    state = deviceImportReducer(state, { type: "SET_MAPPING", index: 0, target_host_id: "host-uuid" });
    expect(state.mappings[0]?.target_host_id).toBe("host-uuid");
  });

  it("pre-fills mappings from host_suggestion for valid_new rows", () => {
    let state = initialDeviceImportState();
    state = deviceImportReducer(state, { type: "UPLOAD_START", bundle: { schema_version: 1 } as never });
    state = deviceImportReducer(state, {
      type: "UPLOAD_OK",
      preview: {
        rows: [
          {
            index: 0,
            device: {},
            status: "valid_new",
            host_suggestion: { id: "host-1", hostname: "lab-04" },
            issues: [],
          },
          { index: 1, device: {}, status: "conflict_skip", host_suggestion: null, issues: [] },
        ],
        available_hosts: [{ id: "host-1", hostname: "lab-04" }],
        bundle_hash: "sha256:x",
        schema_version: 1,
        exported_at: "",
      } as never,
    });
    expect(state.mappings[0]?.target_host_id).toBe("host-1");
    expect(state.mappings[0]?.included).toBe(true);
    expect(state.mappings[1]).toBeUndefined();
  });

  it("RESET returns to initial state", () => {
    let state = initialDeviceImportState();
    state = deviceImportReducer(state, { type: "UPLOAD_START", bundle: { schema_version: 1 } as never });
    state = deviceImportReducer(state, { type: "RESET" });
    expect(state.status).toBe("idle");
    expect(state.bundle).toBeNull();
  });
});
