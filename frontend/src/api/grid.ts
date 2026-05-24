import api from './client';
import type { GridQueueRead, GridStatus, HealthStatus } from '../types';

export async function fetchGridStatus(): Promise<GridStatus> {
  const { data } = await api.get('/grid/status');
  return data;
}

export async function fetchGridQueue(): Promise<GridQueueRead> {
  const { data } = await api.get('/grid/queue');
  return data;
}

export async function fetchHealth(): Promise<HealthStatus> {
  const { data } = await api.get('/health');
  return data;
}
