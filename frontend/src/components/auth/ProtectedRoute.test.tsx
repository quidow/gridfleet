import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import ProtectedRoute from './ProtectedRoute';
import { AuthContext, type AuthContextValue } from '../../context/auth';
import type { AuthSession } from '../../types';

function buildSession(overrides: Partial<AuthSession> = {}): AuthSession {
  return {
    enabled: false,
    authenticated: false,
    username: null,
    csrf_token: null,
    expires_at: null,
    ...overrides,
  };
}

function buildAuthValue(sessionOverrides: Partial<AuthSession> = {}): AuthContextValue {
  const session = buildSession(sessionOverrides);
  return {
    loading: false,
    session,
    enabled: session.enabled,
    authenticated: session.authenticated,
    username: session.username,
    login: vi.fn(),
    logout: vi.fn(),
    probeSession: vi.fn(),
    handleUnauthorized: vi.fn(),
  };
}

function LoginLocationProbe() {
  const location = useLocation();
  return <div>{location.pathname}{location.search}</div>;
}

describe('ProtectedRoute', () => {
  it('renders protected content when auth is disabled', () => {
    render(
      <MemoryRouter initialEntries={['/devices?search=lab']}>
        <AuthContext.Provider value={buildAuthValue()}>
          <Routes>
            <Route element={<ProtectedRoute />}>
              <Route path="/devices" element={<div>Devices Page</div>} />
            </Route>
            <Route path="/login" element={<LoginLocationProbe />} />
          </Routes>
        </AuthContext.Provider>
      </MemoryRouter>,
    );

    expect(screen.getByText('Devices Page')).toBeInTheDocument();
  });

  it('redirects to login with the current deep link when auth is enabled', () => {
    render(
      <MemoryRouter initialEntries={['/devices?search=lab']}>
        <AuthContext.Provider
          value={buildAuthValue({
            enabled: true,
            authenticated: false,
          })}
        >
          <Routes>
            <Route element={<ProtectedRoute />}>
              <Route path="/devices" element={<div>Devices Page</div>} />
            </Route>
            <Route path="/login" element={<LoginLocationProbe />} />
          </Routes>
        </AuthContext.Provider>
      </MemoryRouter>,
    );

    expect(screen.getByText('/login?next=%2Fdevices%3Fsearch%3Dlab')).toBeInTheDocument();
  });
});
