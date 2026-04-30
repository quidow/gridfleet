import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import StatCardsRow from './StatCardsRow';

vi.mock('../../hooks/useDevices', () => ({
  useDevices: () => ({ data: [] }),
}));
vi.mock('../../hooks/useHosts', () => ({
  useHosts: () => ({ data: [] }),
}));
vi.mock('../../hooks/useGrid', () => ({
  useGridStatus: () => ({ data: { active_sessions: 0, queue_size: 0, grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 0 } } }),
  useHealth: () => ({ data: null }),
}));

describe('StatCardsRow', () => {
  it('renders exactly three cards in order: Hosts, Devices, Sessions', () => {
    render(
      <MemoryRouter>
        <StatCardsRow />
      </MemoryRouter>,
    );

    const labels = screen.getAllByTestId('stat-card').map((el) => el.querySelector('.heading-label')?.textContent);
    expect(labels).toEqual(['Hosts', 'Devices', 'Sessions']);
    expect(screen.queryByText('Queue size')).toBeNull();
  });

  it('renders no sparkline SVGs', () => {
    const { container } = render(
      <MemoryRouter>
        <StatCardsRow />
      </MemoryRouter>,
    );
    const sparklineSvgs = container.querySelectorAll('svg[role="img"][aria-label*="trend"]');
    expect(sparklineSvgs.length).toBe(0);
  });
});
