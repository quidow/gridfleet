import type { CursorPaginatedResponse, LifecycleIncidentRead } from '../types';
import api from './client';

export interface LifecycleIncidentParams {
  limit?: number;
  device_id?: string;
  cursor?: string;
  direction?: 'older' | 'newer';
}

export async function fetchLifecycleIncidents(
  params?: LifecycleIncidentParams,
): Promise<CursorPaginatedResponse<LifecycleIncidentRead>> {
  const { data } = await api.get('/lifecycle/incidents', { params });
  return data;
}

export async function fetchRecentLifecycleIncidents(params?: {
  limit?: number;
  device_id?: string;
}): Promise<LifecycleIncidentRead[]> {
  const { data } = await api.get('/lifecycle/incidents', { params });
  return data.items;
}
