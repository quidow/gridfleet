import { useQuery } from '@tanstack/react-query';
import { fetchNotifications } from '../api/notifications';
import type { NotificationListParams } from '../types';

export function useNotifications(params?: NotificationListParams) {
  return useQuery({
    queryKey: ['notifications', params],
    queryFn: () => fetchNotifications(params),
    refetchInterval: 30_000,
  });
}
