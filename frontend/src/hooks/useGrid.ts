import { useQuery } from '@tanstack/react-query';
import { fetchGridStatus, fetchHealth } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useGridStatus() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['grid-status'],
    queryFn: fetchGridStatus,
    ...sseAdaptivePolling(connected, 10_000),
    refetchOnWindowFocus: false,
  });
}

export function useHealth() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    ...sseAdaptivePolling(connected, 30_000),
  });
}
