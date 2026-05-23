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
    meta: { handleErrorLocally: true },
  });
}

const RECENT_INCIDENTS_FALLBACK_POLL_MS = 10_000;
const RECENT_INCIDENTS_CONNECTED_POLL_MS = 60_000;

export function useRecentLifecycleIncidents(params?: { limit?: number; device_id?: string }) {
  const { connected } = useEventStreamStatus();
  const interval = connected ? RECENT_INCIDENTS_CONNECTED_POLL_MS : RECENT_INCIDENTS_FALLBACK_POLL_MS;
  return useQuery({
    queryKey: ['lifecycle', 'incidents', 'recent', params],
    queryFn: () => fetchRecentLifecycleIncidents(params),
    refetchInterval: interval,
    staleTime: interval / 2,
    meta: { handleErrorLocally: true },
  });
}
