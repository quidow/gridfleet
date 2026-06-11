import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { Scorecard } from './Scorecard';

const mockHosts = vi.fn(() => ({ data: [{ id: 'host-1', status: 'online' }] }));
const mockGrid = vi.fn(() => ({
  data: { active_sessions: 0, queue_size: 0, grid: { ready: true }, registry: { device_count: 0 } },
}));
const mockOverview = vi.fn(() => ({ data: { pass_rate_pct: 80, avg_utilization_pct: 1.34, devices_needing_attention: 0 } }));

vi.mock('../../hooks/useHosts', () => ({ useHosts: () => mockHosts() }));
vi.mock('../../hooks/useGrid', () => ({
  useGridStatus: () => mockGrid(),
  useHealth: () => ({ data: { status: 'ok', checks: { database: 'ok' } } }),
}));
vi.mock('../../hooks/useAnalytics', () => ({ useFleetOverview: () => mockOverview() }));
vi.mock('../../hooks/useRolling7DayParams', () => ({
  useRolling7DayParams: () => ({ date_from: '2026-06-04T00:00:00Z', date_to: '2026-06-11T00:00:00Z' }),
}));

function renderScorecard() {
  return render(
    <MemoryRouter>
      <Scorecard />
    </MemoryRouter>,
  );
}

describe('Scorecard', () => {
  it('renders four linked cells', () => {
    renderScorecard();
    for (const label of ['Hosts', 'Sessions', 'Pass rate · 7d', 'Utilization · 7d']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText('Hosts').closest('a')!.getAttribute('href')).toBe('/hosts');
    expect(screen.getByText('Sessions').closest('a')!.getAttribute('href')).toBe('/sessions');
    expect(screen.getByText('80%')).toBeInTheDocument();
    expect(screen.queryByText('Devices')).not.toBeInTheDocument();
    expect(screen.queryByText('Needs attention')).not.toBeInTheDocument();
  });

  it('rounds utilization', () => {
    renderScorecard();
    expect(screen.getByText('1%')).toBeInTheDocument();
  });

  it('flags degraded hosts in the hint', () => {
    mockHosts.mockReturnValueOnce({
      data: [
        { id: 'host-1', status: 'online' },
        { id: 'host-2', status: 'offline' },
      ],
    });
    renderScorecard();
    expect(screen.getByText('1 of 2 online')).toBeInTheDocument();
  });

  it('renders dashes when analytics is unavailable', () => {
    mockOverview.mockReturnValueOnce({ data: undefined as never });
    renderScorecard();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1);
  });
});
