import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  fetchLifecycleIncidents,
  fetchRecentLifecycleIncidents,
  type LifecycleIncidentParams,
} from '../api/lifecycle';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useLifecycleIncidents(params?: LifecycleIncidentParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['lifecycle', 'incidents', params],
    queryFn: () => fetchLifecycleIncidents(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, 10_000)),
    placeholderData: keepPreviousData,
  });
}

export function useRecentLifecycleIncidents(params?: { limit?: number; device_id?: string }) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['lifecycle', 'incidents', 'recent', params],
    queryFn: () => fetchRecentLifecycleIncidents(params),
    ...sseAdaptivePolling(connected, 10_000),
  });
}
