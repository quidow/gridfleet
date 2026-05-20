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

const RECENT_INCIDENTS_POLL_MS = 10_000;

export function useRecentLifecycleIncidents(params?: { limit?: number; device_id?: string }) {
  return useQuery({
    queryKey: ['lifecycle', 'incidents', 'recent', params],
    queryFn: () => fetchRecentLifecycleIncidents(params),
    refetchInterval: RECENT_INCIDENTS_POLL_MS,
    staleTime: RECENT_INCIDENTS_POLL_MS / 2,
  });
}
