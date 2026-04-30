import api from './client';
import type { CursorPaginatedResponse, SessionDetail, SessionListParams } from '../types';

export async function fetchSessions(params?: SessionListParams): Promise<CursorPaginatedResponse<SessionDetail>> {
  const { data } = await api.get('/sessions', { params });
  return data;
}
