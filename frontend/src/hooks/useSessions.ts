import { useQuery } from '@tanstack/react-query';
import { fetchSessions } from '../api/sessions';
import type { SessionListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';

export function useSessions(params?: SessionListParams) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['sessions', 'cursor', params],
    queryFn: () => fetchSessions(params),
    refetchInterval: isHistorical ? false : (connected ? 60_000 : 10_000),
    refetchOnWindowFocus: false,
  });
}
