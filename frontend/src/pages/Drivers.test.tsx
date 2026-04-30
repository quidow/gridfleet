import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import Drivers from './Drivers';

vi.mock('../hooks/useDriverPacks', () => ({
  useDriverPackCatalog: () => ({
    data: [
      {
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        state: 'enabled',
        current_release: '2026.04.0',
        platforms: [],
        appium_server: {
          source: 'npm',
          package: 'appium',
          version: '>=2.5,<3',
          recommended: '2.11.5',
          known_bad: [],
        },
        appium_driver: {
          source: 'npm',
          package: 'appium-uiautomator2-driver',
          version: '>=3,<5',
          recommended: '3.6.0',
          known_bad: [],
        },
        runtime_summary: {
          installed_hosts: 1,
          blocked_hosts: 0,
          actual_appium_server_versions: ['2.19.0'],
          actual_appium_driver_versions: ['4.2.0'],
          driver_drift_hosts: 1,
        },
        runtime_policy: { strategy: 'recommended' },
        active_runs: 0,
        live_sessions: 0,
      },
    ],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

vi.mock('../components/settings/AddDriverDialog', () => ({
  AddDriverDialog: () => null,
}));

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

it('renders page header', () => {
  render(<Drivers />, { wrapper });
  expect(screen.getByText('Driver Packs')).toBeInTheDocument();
});

it('renders pack in table', () => {
  render(<Drivers />, { wrapper });
  expect(screen.getByText('Appium UiAutomator2')).toBeInTheDocument();
  expect(screen.getByText('2026.04.0')).toBeInTheDocument();
});

it('renders recommended and actual runtime versions in the list', () => {
  render(<Drivers />, { wrapper });
  expect(screen.getByText('server rec 2.11.5')).toBeInTheDocument();
  expect(screen.getByText('server actual 2.19.0')).toBeInTheDocument();
  expect(screen.getByText('driver rec 3.6.0')).toBeInTheDocument();
  expect(screen.getByText('driver actual 4.2.0')).toBeInTheDocument();
});

it('renders upload button', () => {
  render(<Drivers />, { wrapper });
  expect(screen.getByRole('button', { name: /upload/i })).toBeInTheDocument();
});
