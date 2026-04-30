import { useQuery } from '@tanstack/react-query';
import { fetchGridStatus, fetchHealth } from '../api/grid';
import { useEventStreamStatus } from '../context/EventStreamContext';

export function useGridStatus() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['grid-status'],
    queryFn: fetchGridStatus,
    refetchInterval: connected ? 60_000 : 10_000,
    refetchOnWindowFocus: false,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 30_000,
  });
}
