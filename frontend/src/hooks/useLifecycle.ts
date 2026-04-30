import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  fetchLifecycleIncidents,
  fetchRecentLifecycleIncidents,
  type LifecycleIncidentParams,
} from '../api/lifecycle';
import { useEventStreamStatus } from '../context/EventStreamContext';

export function useLifecycleIncidents(params?: LifecycleIncidentParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['lifecycle', 'incidents', params],
    queryFn: () => fetchLifecycleIncidents(params),
    refetchInterval: isHistorical ? false : connected ? 60_000 : 10_000,
    placeholderData: keepPreviousData,
  });
}

export function useRecentLifecycleIncidents(params?: { limit?: number; device_id?: string }) {
  return useQuery({
    queryKey: ['lifecycle', 'incidents', 'recent', params],
    queryFn: () => fetchRecentLifecycleIncidents(params),
    staleTime: 30_000,
  });
}
