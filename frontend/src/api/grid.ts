import api from './client';
import type { GridQueueRead, GridStatus, HealthStatus } from '../types';
import type { GridRouterRead } from '../types/gridRouter';

export async function fetchGridStatus(): Promise<GridStatus> {
  const { data } = await api.get('/grid/status');
  return data;
}

export async function fetchGridQueue(): Promise<GridQueueRead> {
  const { data } = await api.get('/grid/queue');
  return data;
}

export async function fetchGridRouter(): Promise<GridRouterRead> {
  const { data } = await api.get('/grid/router');
  return data;
}

export async function fetchHealth(): Promise<HealthStatus> {
  // /api/health signals degraded with a 503 plus a full payload — that body is
  // data the dashboard renders (DB pill, degraded states), not a transport error.
  const { data } = await api.get('/health', {
    validateStatus: (status) => (status >= 200 && status < 300) || status === 503,
  });
  return data;
}
