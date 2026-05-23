import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  exportDeviceDiagnostics,
  fetchDeviceDiagnosticSnapshot,
  listDeviceDiagnosticSnapshots,
} from '../api/deviceDiagnostics';
import { useEventStreamStatus } from '../context/EventStreamContext';

const DIAGNOSTIC_SNAPSHOTS_POLL_MS = 15_000;

export function useDeviceDiagnosticSnapshots(deviceId: string, limit = 5) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device-diagnostic-snapshots', deviceId, limit],
    queryFn: () => listDeviceDiagnosticSnapshots(deviceId, { limit }),
    enabled: Boolean(deviceId),
    refetchInterval: connected ? 60_000 : DIAGNOSTIC_SNAPSHOTS_POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: connected ? 30_000 : DIAGNOSTIC_SNAPSHOTS_POLL_MS / 2,
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
    // Snapshot payload is immutable once written — no polling.
    refetchInterval: false,
    staleTime: Infinity,
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
