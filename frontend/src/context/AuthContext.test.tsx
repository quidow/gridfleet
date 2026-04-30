import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProtectedRoute from '../components/auth/ProtectedRoute';
import { AuthProvider } from './AuthContext';
import { useAuth } from './auth';

const fetchAuthSession = vi.fn();
const configureApiAuth = vi.fn();

vi.mock('../api/auth', () => ({
  fetchAuthSession: (...args: unknown[]) => fetchAuthSession(...args),
  loginWithPassword: vi.fn(),
  logoutSession: vi.fn(),
}));

vi.mock('../api/client', () => ({
  configureApiAuth: (...args: unknown[]) => configureApiAuth(...args),
}));

function LoginProbe() {
  const location = useLocation();
  const auth = useAuth();
  return (
    <div>
      <div data-testid="location">
        {location.pathname}
        {location.search}
      </div>
      <div data-testid="auth-state">{auth.enabled ? 'enabled' : 'disabled'}</div>
    </div>
  );
}

function renderAuthFlow(initialEntry = '/devices?search=lab') {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <AuthProvider>
          <Routes>
            <Route element={<ProtectedRoute />}>
              <Route path="/devices" element={<div>Devices Page</div>} />
            </Route>
            <Route path="/login" element={<LoginProbe />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AuthProvider', () => {
  beforeEach(() => {
    fetchAuthSession.mockReset();
    configureApiAuth.mockReset();
  });

  it('fails closed and redirects to login when session bootstrap fails', async () => {
    fetchAuthSession.mockRejectedValueOnce(new Error('bootstrap failed'));

    renderAuthFlow();

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/login?next=%2Fdevices%3Fsearch%3Dlab');
    });
    expect(screen.getByTestId('auth-state')).toHaveTextContent('enabled');
    expect(screen.queryByText('Devices Page')).not.toBeInTheDocument();
  });
});
