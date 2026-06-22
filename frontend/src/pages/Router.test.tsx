import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { RouterPage } from './Router';
import { useGridRouter } from '../hooks/useGridRouter';
import type { GridRouterRead } from '../types/gridRouter';

vi.mock('../hooks/useGridRouter', () => ({ useGridRouter: vi.fn() }));

const data: GridRouterRead = {
  counts: {
    registered: 2,
    running: 1,
    available: 1,
    busy: 1,
    verifying: 0,
    offline: 0,
    maintenance: 0,
    eligible: 1,
    active_sessions: 1,
    queue_depth: 1,
  },
  nodes: [
    {
      device_id: 'd1',
      device_name: 'Pixel 7',
      platform_id: 'android_mobile',
      host_id: 'h1',
      host_name: 'host-a',
      operational_state: 'available',
      node_effective_state: 'running',
      session_id: null,
      session_target: null,
      stereotype: { platformName: 'Android', 'gridfleet:deviceId': '7f3a' },
    },
    {
      device_id: 'd2',
      device_name: 'iPhone 15',
      platform_id: 'ios',
      host_id: 'h2',
      host_name: 'host-b',
      operational_state: 'busy',
      node_effective_state: 'running',
      session_id: 's_4821',
      session_target: 'http://host-b:8100',
      stereotype: { platformName: 'iOS', 'gridfleet:deviceId': '2b9c' },
    },
  ],
  queue: [{ requestId: 'q1', capabilities: { platformName: 'Android' }, requestTimestamp: new Date().toISOString(), runId: null }],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RouterPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('RouterPage', () => {
  it('renders counts, node cards, and the queue', () => {
    vi.mocked(useGridRouter).mockReturnValue({
      data,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
      dataUpdatedAt: Date.now(),
    } as unknown as ReturnType<typeof useGridRouter>);
    renderPage();
    expect(screen.getByRole('heading', { name: 'Router' })).toBeInTheDocument();
    expect(screen.getByText('Pixel 7')).toBeInTheDocument();
    expect(screen.getByText('iPhone 15')).toBeInTheDocument();
    expect(screen.getByText('open')).toBeInTheDocument();
    expect(screen.getByText('Queue (1)')).toBeInTheDocument();
  });
});
