import { useQuery } from '@tanstack/react-query';
import { fetchGridQueue } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useGridQueue(options?: { enabled?: boolean }) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['grid-queue'],
    queryFn: fetchGridQueue,
    ...sseAdaptivePolling(connected, 5_000),
    refetchOnWindowFocus: false,
    enabled: options?.enabled,
  });
}
