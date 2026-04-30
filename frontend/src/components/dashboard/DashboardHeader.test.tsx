import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import DashboardHeader from './DashboardHeader';

vi.mock('../../hooks/useDevices', () => ({
  useDevices: () => ({ data: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], dataUpdatedAt: 0 }),
}));
vi.mock('../../hooks/useHosts', () => ({
  useHosts: () => ({ data: [{ id: 'h1', status: 'online' }], dataUpdatedAt: 0 }),
}));
vi.mock('../../hooks/useGrid', () => ({
  useGridStatus: () => ({
    data: { grid: { ready: true }, registry: { device_count: 0, devices: [] }, active_sessions: 0, queue_size: 0 },
    dataUpdatedAt: 0,
  }),
  useHealth: () => ({ data: { status: 'healthy', checks: { database: 'ok' } }, dataUpdatedAt: 0 }),
}));
vi.mock('../../context/EventStreamContext', () => ({
  useEventStreamStatus: () => ({ connected: true }),
}));

describe('DashboardHeader', () => {
  it('renders title, subtitle, and three system-health pills in the summary slot', () => {
    render(
      <MemoryRouter>
        <DashboardHeader />
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    expect(screen.getByText(/Fleet overview/)).toBeInTheDocument();
    expect(screen.getAllByTestId('system-health-pill')).toHaveLength(3);
    expect(screen.getByText('Stream')).toBeInTheDocument();
    expect(screen.getByText('DB')).toBeInTheDocument();
    expect(screen.getByText('Grid')).toBeInTheDocument();
  });
});
