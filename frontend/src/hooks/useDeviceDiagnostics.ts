import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  exportDeviceDiagnostics,
  fetchDeviceDiagnosticSnapshot,
  listDeviceDiagnosticSnapshots,
} from '../api/deviceDiagnostics';

export function useDeviceDiagnosticSnapshots(deviceId: string, limit = 5) {
  return useQuery({
    queryKey: ['device-diagnostic-snapshots', deviceId, limit],
    queryFn: () => listDeviceDiagnosticSnapshots(deviceId, { limit }),
    enabled: Boolean(deviceId),
  });
}

export function useDeviceDiagnosticSnapshot(
  deviceId: string,
  snapshotId: string | null,
  redact: boolean,
) {
  return useQuery({
    queryKey: ['device-diagnostic-snapshot', deviceId, snapshotId, redact],
    queryFn: () => fetchDeviceDiagnosticSnapshot(deviceId, snapshotId!, { redact }),
    enabled: Boolean(deviceId && snapshotId),
  });
}

export function useExportDeviceDiagnostics(deviceId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (options: { redact?: boolean; persist?: boolean } = {}) =>
      exportDeviceDiagnostics(deviceId, options),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['device-diagnostic-snapshots', deviceId],
      });
    },
  });
}
