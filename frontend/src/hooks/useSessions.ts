import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchSessions, killSession } from '../api/sessions';
import type { SessionListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useSessions(params?: SessionListParams, pollMs = 10_000) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: ['sessions', 'cursor', params],
    queryFn: () => fetchSessions(params),
    ...(isHistorical ? { refetchInterval: false as const, staleTime: Infinity } : sseAdaptivePolling(connected, pollMs)),
    refetchOnWindowFocus: false,
  });
}

export function useKillSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => killSession(sessionId),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}
