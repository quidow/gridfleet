import { useCallback, useEffect, useReducer } from "react";

import api from "../api/client";
import {
  commitImport,
  validateImportBundle,
  type ExportBundle,
  type ImportCommitResult,
  type ImportPreview,
} from "../api/devicesPortability";

export type ImportStatus = "idle" | "validating" | "reviewing" | "committing" | "done" | "error";

export interface RowMapping {
  target_host_id: string;
  included: boolean;
}

export interface DeviceImportState {
  status: ImportStatus;
  bundle: ExportBundle | null;
  preview: ImportPreview | null;
  mappings: Record<number, RowMapping>;
  result: ImportCommitResult | null;
  errorMessage: string | null;
}

export type DeviceImportAction =
  | { type: "UPLOAD_START"; bundle: ExportBundle }
  | { type: "UPLOAD_OK"; preview: ImportPreview }
  | { type: "UPLOAD_FAIL"; message: string }
  | { type: "SET_MAPPING"; index: number; target_host_id: string }
  | { type: "TOGGLE_INCLUDED"; index: number }
  | { type: "COMMIT_START" }
  | { type: "COMMIT_OK"; result: ImportCommitResult }
  | { type: "COMMIT_FAIL"; message: string }
  | { type: "RESET" };

export function initialDeviceImportState(): DeviceImportState {
  return {
    status: "idle",
    bundle: null,
    preview: null,
    mappings: {},
    result: null,
    errorMessage: null,
  };
}

export function deviceImportReducer(
  state: DeviceImportState,
  action: DeviceImportAction,
): DeviceImportState {
  switch (action.type) {
    case "UPLOAD_START":
      return { ...state, status: "validating", bundle: action.bundle, errorMessage: null };
    case "UPLOAD_OK": {
      const mappings: Record<number, RowMapping> = {};
      for (const row of action.preview.rows) {
        if (row.status === "valid_new") {
          mappings[row.index] = {
            target_host_id: row.host_suggestion?.id ?? "",
            included: true,
          };
        }
      }
      return { ...state, status: "reviewing", preview: action.preview, mappings };
    }
    case "UPLOAD_FAIL":
      return { ...state, status: "error", errorMessage: action.message };
    case "SET_MAPPING":
      return {
        ...state,
        mappings: {
          ...state.mappings,
          [action.index]: {
            target_host_id: action.target_host_id,
            included: state.mappings[action.index]?.included ?? true,
          },
        },
      };
    case "TOGGLE_INCLUDED": {
      const current = state.mappings[action.index];
      if (!current) return state;
      return {
        ...state,
        mappings: { ...state.mappings, [action.index]: { ...current, included: !current.included } },
      };
    }
    case "COMMIT_START":
      return { ...state, status: "committing", errorMessage: null };
    case "COMMIT_OK":
      return { ...state, status: "done", result: action.result };
    case "COMMIT_FAIL":
      return { ...state, status: "error", errorMessage: action.message };
    case "RESET":
      return initialDeviceImportState();
  }
}

export function useDeviceImport() {
  const [state, dispatch] = useReducer(deviceImportReducer, undefined, initialDeviceImportState);

  useEffect(() => {
    if (
      state.status === "validating" ||
      state.status === "reviewing" ||
      state.status === "committing"
    ) {
      const handler = (e: BeforeUnloadEvent): void => {
        e.preventDefault();
        e.returnValue = "";
      };
      window.addEventListener("beforeunload", handler);
      return () => window.removeEventListener("beforeunload", handler);
    }
    return undefined;
  }, [state.status]);

  const upload = useCallback(async (bundle: ExportBundle): Promise<void> => {
    dispatch({ type: "UPLOAD_START", bundle });
    try {
      const preview = await validateImportBundle(api, bundle);
      dispatch({ type: "UPLOAD_OK", preview });
    } catch (e) {
      dispatch({ type: "UPLOAD_FAIL", message: (e as Error).message });
    }
  }, []);

  const commit = useCallback(async (): Promise<void> => {
    if (!state.bundle || !state.preview) return;
    dispatch({ type: "COMMIT_START" });
    try {
      const mappings = Object.entries(state.mappings)
        .filter(([, m]) => m.included && m.target_host_id)
        .map(([idx, m]) => ({ index: Number(idx), target_host_id: m.target_host_id }));
      const result = await commitImport(api, {
        bundle: state.bundle,
        bundle_hash: state.preview.bundle_hash,
        mappings,
      });
      dispatch({ type: "COMMIT_OK", result });
    } catch (e) {
      dispatch({ type: "COMMIT_FAIL", message: (e as Error).message });
    }
  }, [state.bundle, state.preview, state.mappings]);

  const reset = useCallback(() => dispatch({ type: "RESET" }), []);

  return { state, upload, commit, reset, dispatch };
}
