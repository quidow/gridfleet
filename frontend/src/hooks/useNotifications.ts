import { useQuery } from '@tanstack/react-query';
import { fetchNotifications } from '../api/notifications';
import type { NotificationListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';

export function useNotifications(params?: NotificationListParams) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['notifications', params],
    queryFn: () => fetchNotifications(params),
    ...sseAdaptivePolling(connected, 30_000),
  });
}
