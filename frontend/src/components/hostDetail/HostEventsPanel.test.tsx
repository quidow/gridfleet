import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import HostEventsPanel from './HostEventsPanel';
import { useHostEvents } from '../../hooks/useHosts';

vi.mock('../../hooks/useHosts', () => ({
  useHostEvents: vi.fn(),
}));

describe('HostEventsPanel', () => {
  beforeEach(() => {
    vi.mocked(useHostEvents).mockReturnValue({
      data: {
        events: [
          {
            event_id: '1',
            type: 'host.status_changed',
            ts: '2026-05-15T10:00:00Z',
            data: { host_id: 'host-1', old_status: 'online', new_status: 'degraded' },
          },
        ],
        total: 1,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useHostEvents>);
  });

  it('renders an event row', () => {
    render(<HostEventsPanel hostId="host-1" />);
    expect(screen.getByText('host.status_changed')).toBeInTheDocument();
  });

  it('expands row to show JSON payload on click', () => {
    render(<HostEventsPanel hostId="host-1" />);
    fireEvent.click(screen.getByRole('button', { name: /host.status_changed/i }));
    expect(screen.getByText(/old_status/)).toBeInTheDocument();
  });

  it('renders empty state when no events', () => {
    vi.mocked(useHostEvents).mockReturnValueOnce({
      data: { events: [], total: 0 },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useHostEvents>);
    render(<HostEventsPanel hostId="host-1" />);
    expect(screen.getByText(/no events/i)).toBeInTheDocument();
  });
});
