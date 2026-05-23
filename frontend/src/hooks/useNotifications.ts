import { useQuery } from '@tanstack/react-query';
import { fetchNotifications } from '../api/notifications';
import type { NotificationListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';

export function useNotifications(params?: NotificationListParams) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['notifications', params],
    queryFn: () => fetchNotifications(params),
    refetchInterval: connected ? 60_000 : 30_000,
    staleTime: connected ? 30_000 : 15_000,
  });
}
