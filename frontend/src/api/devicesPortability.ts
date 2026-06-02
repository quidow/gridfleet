import type { AxiosInstance } from "axios";

import api from "./client";
import type { components } from "./openapi";

export type ExportBundle = components["schemas"]["ExportBundle"];
export type ImportPreview = components["schemas"]["ImportPreview"];
export type ImportCommitRequest = components["schemas"]["ImportCommitRequest"];
export type ImportCommitResult = components["schemas"]["ImportCommitResult"];

export async function fetchExportBundle(client: AxiosInstance = api): Promise<ExportBundle> {
  const response = await client.get<ExportBundle>("/portability/export", { responseType: "json" });
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
