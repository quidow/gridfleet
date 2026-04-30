import { createContext, useContext } from 'react';
import type { AuthSession } from '../types';

export type LoginRequest = {
  username: string;
  password: string;
};

export interface AuthContextValue {
  loading: boolean;
  session: AuthSession;
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
  login: (request: LoginRequest) => Promise<AuthSession>;
  logout: () => Promise<void>;
  probeSession: () => Promise<AuthSession>;
  handleUnauthorized: () => void;
}

export const DEFAULT_SESSION: AuthSession = {
  enabled: false,
  authenticated: false,
  username: null,
  csrf_token: null,
  expires_at: null,
};

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth() {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return value;
}
