import { useQuery } from '@tanstack/react-query';
import { fetchGridStatus, fetchHealth } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_DEFAULT_MS, POLL_SLOW_MS, sseAdaptivePolling } from './polling';

export function useGridStatus() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.gridStatus.root,
    queryFn: fetchGridStatus,
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
    refetchOnWindowFocus: false,
  });
}

export function useHealth() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.health.root,
    queryFn: fetchHealth,
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}
