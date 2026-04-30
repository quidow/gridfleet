import api from './client';
import type { NotificationListParams, NotificationListResponse } from '../types';

export async function fetchNotifications(params?: NotificationListParams): Promise<NotificationListResponse> {
  const queryParams: Record<string, string> = {};
  if (params?.limit !== undefined) queryParams.limit = String(params.limit);
  if (params?.offset !== undefined) queryParams.offset = String(params.offset);
  if (params?.types?.length) queryParams.types = params.types.join(',');
  const { data } = await api.get('/notifications', { params: queryParams });
  return data;
}
