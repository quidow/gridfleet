import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import SystemHealthPills from './SystemHealthPills';

const mockGridStatus = vi.fn(() => ({
  data: {
    grid: { ready: true, value: { ready: true, nodes: [] } },
    registry: { device_count: 0 },
    active_sessions: 0,
    queue_size: 0,
  },
}));
const mockEventStream = vi.fn(() => ({ connected: true }));

vi.mock('../../hooks/useGrid', () => ({
  useGridStatus: () => mockGridStatus(),
  useHealth: () => ({ data: { status: 'ok', checks: { database: 'ok' } } }),
}));

vi.mock('../../hooks/useHosts', () => ({
  useHosts: () => ({ data: [] }),
}));

vi.mock('../../context/EventStreamContext', () => ({
  useEventStreamStatus: () => mockEventStream(),
}));

describe('SystemHealthPills', () => {
  it('renders three pills: Stream, DB, Grid', () => {
    render(
      <MemoryRouter>
        <SystemHealthPills />
      </MemoryRouter>,
    );
    const pills = screen.getAllByTestId('system-health-pill');
    expect(pills).toHaveLength(3);
    expect(screen.getByText('Stream')).toBeInTheDocument();
    expect(screen.getByText('DB')).toBeInTheDocument();
    expect(screen.getByText('Grid')).toBeInTheDocument();
  });

  it('shows Live + OK + Ready when everything healthy', () => {
    render(
      <MemoryRouter>
        <SystemHealthPills />
      </MemoryRouter>,
    );
    expect(screen.getByText('Live')).toBeInTheDocument();
    expect(screen.getByText('OK')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
  });

  it('links Grid pill to /sessions when grid is not ready', () => {
    mockGridStatus.mockReturnValueOnce({
      data: {
        grid: { ready: false, value: { ready: false, nodes: [] } },
        registry: { device_count: 0 },
        active_sessions: 0,
        queue_size: 0,
      },
    });
    render(
      <MemoryRouter>
        <SystemHealthPills />
      </MemoryRouter>,
    );
    const gridLabel = screen.getByText('Grid');
    const link = gridLabel.closest('a');
    expect(link).not.toBeNull();
    expect(link!.getAttribute('href')).toBe('/sessions');
  });

  it('links Stream pill to /settings when disconnected', () => {
    mockEventStream.mockReturnValueOnce({ connected: false });
    render(
      <MemoryRouter>
        <SystemHealthPills />
      </MemoryRouter>,
    );
    const streamLabel = screen.getByText('Stream');
    const link = streamLabel.closest('a');
    expect(link).not.toBeNull();
    expect(link!.getAttribute('href')).toBe('/settings');
  });
});
