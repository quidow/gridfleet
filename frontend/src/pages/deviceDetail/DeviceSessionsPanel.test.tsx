import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { DeviceSessionsPanel } from './DeviceSessionsPanel';

vi.mock('../../api/sessions', () => ({
  fetchSessions: vi.fn(),
}));

vi.mock('../../context/EventStreamContext', () => ({
  useEventStreamStatus: () => ({ connected: false }),
}));

import { fetchSessions } from '../../api/sessions';

const mockFetchSessions = vi.mocked(fetchSessions);

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe('DeviceSessionsPanel', () => {
  it('renders empty state when no sessions', async () => {
    mockFetchSessions.mockResolvedValue({ items: [], limit: 50, next_cursor: null, prev_cursor: null });
    render(<DeviceSessionsPanel deviceId="dev-1" />, { wrapper });
    expect(await screen.findByText(/no sessions/i)).toBeInTheDocument();
  });

  it('passes device_id filter to fetchSessions', async () => {
    mockFetchSessions.mockResolvedValue({ items: [], limit: 50, next_cursor: null, prev_cursor: null });
    render(<DeviceSessionsPanel deviceId="dev-42" />, { wrapper });
    await screen.findByText(/no sessions/i);
    expect(mockFetchSessions).toHaveBeenCalledWith(
      expect.objectContaining({ device_id: 'dev-42' }),
    );
  });
});
