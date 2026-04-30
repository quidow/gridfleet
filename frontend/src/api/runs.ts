import api from './client';
import type { RunDetail, RunListParams, RunListResponse, RunRead } from '../types';

export async function fetchRuns(params?: RunListParams): Promise<RunListResponse> {
  const { data } = await api.get('/runs', { params });
  return data;
}

export async function fetchRun(id: string): Promise<RunDetail> {
  const { data } = await api.get(`/runs/${id}`);
  return data;
}


export async function cancelRun(id: string): Promise<RunRead> {
  const { data } = await api.post(`/runs/${id}/cancel`);
  return data;
}

export async function forceReleaseRun(id: string): Promise<RunRead> {
  const { data } = await api.post(`/runs/${id}/force-release`);
  return data;
}
