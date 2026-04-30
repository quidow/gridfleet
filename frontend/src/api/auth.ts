import api, { authHandlerBypassHeaders } from './client';
import type { AuthSession } from '../types';

export async function fetchAuthSession(): Promise<AuthSession> {
  const { data } = await api.get('/auth/session', {
    headers: authHandlerBypassHeaders(),
  });
  return data;
}

export async function loginWithPassword(username: string, password: string): Promise<AuthSession> {
  const { data } = await api.post(
    '/auth/login',
    { username, password },
    {
      headers: authHandlerBypassHeaders(),
    },
  );
  return data;
}

export async function logoutSession(): Promise<AuthSession> {
  const { data } = await api.post('/auth/logout', undefined, {
    headers: authHandlerBypassHeaders(),
  });
  return data;
}
