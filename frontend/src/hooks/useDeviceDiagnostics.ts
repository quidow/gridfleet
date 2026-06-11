import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  exportDeviceDiagnostics,
  fetchDeviceDiagnosticSnapshot,
  listDeviceDiagnosticSnapshots,
} from '../api/deviceDiagnostics';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_RELAXED_MS, sseAdaptivePolling } from './polling';

export function useDeviceDiagnosticSnapshots(deviceId: string, limit = 5) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceDiagnosticSnapshots.list(deviceId, limit),
    queryFn: () => listDeviceDiagnosticSnapshots(deviceId, { limit }),
    enabled: Boolean(deviceId),
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
    refetchIntervalInBackground: false,
  });
}

export function useDeviceDiagnosticSnapshot(
  deviceId: string,
  snapshotId: string | null,
  redact: boolean,
) {
  return useQuery({
    queryKey: qk.deviceDiagnosticSnapshot.detail(deviceId, snapshotId, redact),
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
        queryKey: qk.deviceDiagnosticSnapshots.byDevice(deviceId),
      });
    },
  });
}
