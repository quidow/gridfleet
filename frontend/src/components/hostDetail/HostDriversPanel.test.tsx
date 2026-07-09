import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { HostDriversPanel } from './HostDriversPanel';

const mockUseHostDriverPacks = vi.fn();
const mockUseDriverPackCatalog = vi.fn();

vi.mock('../../hooks/useDriverPacks', () => ({
  useHostDriverPacks: (...args: unknown[]) => mockUseHostDriverPacks(...args),
  useDriverPackCatalog: (...args: unknown[]) => mockUseDriverPackCatalog(...args),
}));

vi.mock('../../api/driverPacks', () => ({
  triggerDriverDoctor: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

function Wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

describe('HostDriversPanel', () => {
  it('uses Drivers wording for empty host status', () => {
    mockUseHostDriverPacks.mockReturnValue({ data: { packs: [], runtimes: [], doctor: [] }, isLoading: false });
    mockUseDriverPackCatalog.mockReturnValue({ data: [] });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />, { wrapper: Wrapper });

    expect(screen.getByText('No drivers installed. Enable drivers in Settings.')).toBeInTheDocument();
  });

  it('renders an installed pack', () => {
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'appium-xcuitest',
            pack_release: '2026.04.0',
            runtime_id: null,
            status: 'installed',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: null,
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-xcuitest',
          display_name: 'Appium XCUITest',
          state: 'enabled',
          current_release: '2026.04.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
        },
      ],
    });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />, { wrapper: Wrapper });

    expect(screen.getByText('appium-xcuitest')).toBeInTheDocument();
  });
});
