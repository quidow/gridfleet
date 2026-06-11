import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { cancelRun, fetchRun, fetchRuns, forceReleaseRun } from '../api/runs';
import type { RunListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_DEFAULT_MS, POLL_FAST_MS, sseAdaptivePolling } from './polling';

export function useRuns(params?: RunListParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: qk.runs.cursorList(params),
    queryFn: () => fetchRuns(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, POLL_DEFAULT_MS)),
    refetchOnWindowFocus: false,
  });
}

export function useRun(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.run.detail(id),
    queryFn: () => fetchRun(id),
    ...sseAdaptivePolling(connected, POLL_FAST_MS),
    refetchOnWindowFocus: false,
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => cancelRun(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.runs.root });
      qc.invalidateQueries({ queryKey: qk.run.root });
      qc.invalidateQueries({ queryKey: qk.devices.root });
    },
  });
}

export function useForceReleaseRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => forceReleaseRun(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.runs.root });
      qc.invalidateQueries({ queryKey: qk.run.root });
      qc.invalidateQueries({ queryKey: qk.devices.root });
    },
  });
}
