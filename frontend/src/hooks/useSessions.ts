import { useQuery } from '@tanstack/react-query';
import { fetchSessions } from '../api/sessions';
import type { SessionListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useSessions(params?: SessionListParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['sessions', 'cursor', params],
    queryFn: () => fetchSessions(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, 10_000)),
    refetchOnWindowFocus: false,
  });
}
