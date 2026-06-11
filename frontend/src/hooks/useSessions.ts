import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchSessions, killSession } from '../api/sessions';
import type { SessionListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_DEFAULT_MS, sseAdaptivePolling } from './polling';

export function useSessions(params?: SessionListParams, pollMs = POLL_DEFAULT_MS) {
  const { connected } = useEventStreamStatus();
  const isHistorical = Boolean(params?.cursor);
  return useQuery({
    queryKey: qk.sessions.cursorList(params),
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
      void queryClient.invalidateQueries({ queryKey: qk.sessions.root });
    },
  });
}
