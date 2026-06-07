import api from './client';
import type { CursorPaginatedResponse, SessionDetail, SessionKillResult, SessionListParams } from '../types';

export async function fetchSessions(params?: SessionListParams): Promise<CursorPaginatedResponse<SessionDetail>> {
  const { data } = await api.get('/sessions', { params });
  return data;
}

export async function killSession(sessionId: string): Promise<SessionKillResult> {
  const { data } = await api.post(`/sessions/${encodeURIComponent(sessionId)}/kill`);
  return data;
}
