import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  fetchLifecycleIncidents,
  fetchRecentLifecycleIncidents,
  type LifecycleIncidentParams,
} from '../api/lifecycle';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_DEFAULT_MS, sseAdaptivePolling } from './polling';

export function useLifecycleIncidents(params?: LifecycleIncidentParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: qk.lifecycleIncidents.list(params),
    queryFn: () => fetchLifecycleIncidents(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, POLL_DEFAULT_MS)),
    placeholderData: keepPreviousData,
  });
}

export function useRecentLifecycleIncidents(params?: { limit?: number; device_id?: string }) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.lifecycleIncidents.recent(params),
    queryFn: () => fetchRecentLifecycleIncidents(params),
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
  });
}
