import type { AxiosInstance } from "axios";

import api from "./client";
import type { components } from "./openapi";

// The bundle schema is emitted as a request/response pair because group
// filters serialize through a plain model serializer on the way out. The
// request variant carries the structured filter shape, so it is the one the
// import flow parses and posts; the response variant is only downloaded.
export type ExportBundle = components["schemas"]["ExportBundle-Input"];
export type ExportBundleResponse = components["schemas"]["ExportBundle-Output"];
export type ImportPreview = components["schemas"]["ImportPreview"];
export type ImportCommitRequest = components["schemas"]["ImportCommitRequest"];
export type ImportCommitResult = components["schemas"]["ImportCommitResult"];

export async function fetchExportBundle(client: AxiosInstance = api): Promise<ExportBundleResponse> {
  const response = await client.get<ExportBundleResponse>("/portability/export", { responseType: "json" });
  return response.data;
}

export async function downloadExportBundle(): Promise<void> {
  const bundle = await fetchExportBundle();
  const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  triggerBrowserDownload(blob, `gridfleet-devices-${stamp}.json`);
}

export async function validateImportBundle(
  client: AxiosInstance,
  bundle: ExportBundle,
): Promise<ImportPreview> {
  const response = await client.post<ImportPreview>("/portability/import/validate", bundle);
  return response.data;
}

export async function commitImport(
  client: AxiosInstance,
  request: ImportCommitRequest,
): Promise<ImportCommitResult> {
  const response = await client.post<ImportCommitResult>("/portability/import", request);
  return response.data;
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
