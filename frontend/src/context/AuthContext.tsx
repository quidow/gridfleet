import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { fetchAuthSession, loginWithPassword, logoutSession } from '../api/auth';
import { configureApiAuth } from '../api/client';
import { buildLocationTarget, normalizeNextTarget } from '../lib/authRouting';
import type { AuthSession } from '../types';
import { AuthContext, DEFAULT_SESSION, type AuthContextValue, type LoginRequest } from './auth';

const FAIL_CLOSED_SESSION: AuthSession = {
  ...DEFAULT_SESSION,
  enabled: true,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [session, setSession] = useState<AuthSession>(DEFAULT_SESSION);
  const [loading, setLoading] = useState(true);
  const sessionRef = useRef(session);
  const locationRef = useRef(location);
  const handleUnauthorizedRef = useRef<() => void>(() => {});

  useEffect(() => {
    sessionRef.current = session;
  }, [session]);

  useEffect(() => {
    locationRef.current = location;
  }, [location]);

  const redirectToLogin = useCallback(() => {
    const currentLocation = locationRef.current;
    if (currentLocation.pathname === '/login') {
      return;
    }

    const nextTarget = normalizeNextTarget(buildLocationTarget(currentLocation));
    const params = new URLSearchParams();
    if (nextTarget !== '/') {
      params.set('next', nextTarget);
    }

    const suffix = params.toString();
    navigate(`/login${suffix ? `?${suffix}` : ''}`, { replace: true });
  }, [navigate]);

  const clearAuthenticatedState = useCallback((enabled: boolean) => {
    queryClient.clear();
    setSession({
      enabled,
      authenticated: false,
      username: null,
      csrf_token: null,
      expires_at: null,
    });
  }, [queryClient]);

  useEffect(() => {
    handleUnauthorizedRef.current = () => {
      if (!sessionRef.current.enabled) {
        return;
      }
      clearAuthenticatedState(true);
      redirectToLogin();
    };
  }, [clearAuthenticatedState, redirectToLogin]);

  useEffect(() => {
    configureApiAuth({
      getCsrfToken: () => {
        const currentSession = sessionRef.current;
        if (!currentSession.enabled || !currentSession.authenticated) {
          return null;
        }
        return currentSession.csrf_token;
      },
      onUnauthorized: () => handleUnauthorizedRef.current(),
    });

    return () => {
      configureApiAuth({ getCsrfToken: null, onUnauthorized: null });
    };
  }, []);

  useEffect(() => {
    let active = true;

    async function bootstrap() {
      try {
        const nextSession = await fetchAuthSession();
        if (!active) {
          return;
        }
        setSession(nextSession);
      } catch {
        if (!active) {
          return;
        }
        // If bootstrap cannot confirm the current session, fail closed so the SPA
        // does not briefly behave like auth is disabled.
        setSession(FAIL_CLOSED_SESSION);
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void bootstrap();

    return () => {
      active = false;
    };
  }, []);

  async function login(request: LoginRequest): Promise<AuthSession> {
    const nextSession = await loginWithPassword(request.username, request.password);
    setSession(nextSession);
    return nextSession;
  }

  async function logout(): Promise<void> {
    const authEnabled = sessionRef.current.enabled;
    if (authEnabled) {
      try {
        await logoutSession();
      } catch {
        // The frontend still clears local state even if the server-side session already expired.
      }
    }

    clearAuthenticatedState(authEnabled);
    navigate(authEnabled ? '/login' : '/', { replace: true });
  }

  async function probeSession(): Promise<AuthSession> {
    try {
      const nextSession = await fetchAuthSession();
      setSession(nextSession);
      if (nextSession.enabled && !nextSession.authenticated) {
        handleUnauthorizedRef.current();
      }
      return nextSession;
    } catch {
      return sessionRef.current;
    }
  }

  const value: AuthContextValue = {
    loading,
    session,
    enabled: session.enabled,
    authenticated: session.authenticated,
    username: session.username,
    login,
    logout,
    probeSession,
    handleUnauthorized: () => handleUnauthorizedRef.current(),
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
