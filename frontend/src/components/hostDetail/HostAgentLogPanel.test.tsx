import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import HostAgentLogPanel from './HostAgentLogPanel';
import { useHostAgentLogs } from '../../hooks/useHosts';

vi.mock('../../hooks/useHosts', () => ({
  useHostAgentLogs: vi.fn(),
}));

describe('HostAgentLogPanel', () => {
  beforeEach(() => {
    vi.mocked(useHostAgentLogs).mockReturnValue({
      data: {
        lines: [
          {
            ts: '2026-05-15T10:00:00Z',
            level: 'INFO',
            logger_name: 'agent.test',
            message: 'hello world',
            sequence_no: 0,
            boot_id: '00000000-0000-0000-0000-000000000001',
          },
        ],
        total: 1,
        has_more: false,
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useHostAgentLogs>);
  });

  it('renders a log line', () => {
    render(<HostAgentLogPanel hostId="host-1" />);
    expect(screen.getByText(/hello world/)).toBeInTheDocument();
  });

  it('renders an empty-state message when no lines', () => {
    vi.mocked(useHostAgentLogs).mockReturnValueOnce({
      data: { lines: [], total: 0, has_more: false },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useHostAgentLogs>);
    render(<HostAgentLogPanel hostId="host-1" />);
    expect(screen.getByText(/no logs received yet/i)).toBeInTheDocument();
  });

  it('shows INFO+/WARN+/ERROR+ level filters', () => {
    render(<HostAgentLogPanel hostId="host-1" />);
    expect(screen.getByLabelText(/level/i)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'INFO+' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'WARN+' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'ERROR+' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'DEBUG' })).toBeNull();
  });
});
