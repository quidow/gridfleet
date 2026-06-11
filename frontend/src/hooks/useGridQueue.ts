import { useQuery } from '@tanstack/react-query';
import { fetchGridQueue } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_FAST_MS, sseAdaptivePolling } from './polling';

export function useGridQueue(options?: { enabled?: boolean }) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.gridQueue.root,
    queryFn: fetchGridQueue,
    ...sseAdaptivePolling(connected, POLL_FAST_MS),
    refetchOnWindowFocus: false,
    enabled: options?.enabled,
  });
}
