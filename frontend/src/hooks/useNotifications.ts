import { useQuery } from '@tanstack/react-query';
import { fetchNotifications } from '../api/notifications';
import type { NotificationListParams } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_SLOW_MS, sseAdaptivePolling } from './polling';

export function useNotifications(params?: NotificationListParams) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.notifications.list(params),
    queryFn: () => fetchNotifications(params),
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}
