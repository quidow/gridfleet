import api from './client';
import type { components } from './openapi';

export type DiagnosticExportResponse = components['schemas']['DiagnosticExportResponse'];
export type DiagnosticSnapshotDetail = components['schemas']['DiagnosticSnapshotDetail'];
export type DiagnosticSnapshotListResponse = components['schemas']['DiagnosticSnapshotListResponse'];

export async function exportDeviceDiagnostics(
  deviceId: string,
  options: { redact?: boolean; persist?: boolean } = {},
): Promise<DiagnosticExportResponse> {
  const { data } = await api.post(`/devices/${deviceId}/diagnostics/export`, null, {
    params: {
      redact: options.redact ?? false,
      persist: options.persist ?? true,
    },
  });
  return data;
}

export async function listDeviceDiagnosticSnapshots(
  deviceId: string,
  options: { limit?: number; before?: string | null } = {},
): Promise<DiagnosticSnapshotListResponse> {
  const { data } = await api.get(`/devices/${deviceId}/diagnostics/snapshots`, {
    params: {
      limit: options.limit ?? 20,
      before: options.before ?? undefined,
    },
  });
  return data;
}

export async function fetchDeviceDiagnosticSnapshot(
  deviceId: string,
  snapshotId: string,
  options: { redact?: boolean } = {},
): Promise<DiagnosticSnapshotDetail> {
  const { data } = await api.get(`/devices/${deviceId}/diagnostics/snapshots/${snapshotId}`, {
    params: { redact: options.redact ?? false },
  });
  return data;
}
