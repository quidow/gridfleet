import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { cancelRun, fetchRun, fetchRuns, forceReleaseRun } from '../api/runs';
import type { RunListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useRuns(params?: RunListParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['runs', 'cursor', params],
    queryFn: () => fetchRuns(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, 10_000)),
    refetchOnWindowFocus: false,
  });
}

export function useRun(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['run', id],
    queryFn: () => fetchRun(id),
    ...sseAdaptivePolling(connected, 5_000),
    refetchOnWindowFocus: false,
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => cancelRun(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] });
      qc.invalidateQueries({ queryKey: ['run'] });
      qc.invalidateQueries({ queryKey: ['devices'] });
    },
  });
}

export function useForceReleaseRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => forceReleaseRun(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] });
      qc.invalidateQueries({ queryKey: ['run'] });
      qc.invalidateQueries({ queryKey: ['devices'] });
    },
  });
}
