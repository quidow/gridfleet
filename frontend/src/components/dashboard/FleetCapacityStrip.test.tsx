import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FleetCapacityStrip from './FleetCapacityStrip';
import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';

vi.mock('../../hooks/useAnalytics', () => ({
  useFleetCapacityTimeline: vi.fn(),
}));

const mockedUseFleetCapacityTimeline = vi.mocked(useFleetCapacityTimeline);

const timeline = {
  date_from: '2026-04-19T00:00:00Z',
  date_to: '2026-04-19T00:30:00Z',
  bucket_minutes: 15,
  series: [
    {
      timestamp: '2026-04-19T00:00:00Z',
      devices_total: 3,
      devices_available: 1,
      devices_offline: 1,
      devices_maintenance: 0,
      hosts_total: 1,
      hosts_online: 1,
      active_sessions: 0,
      queued_requests: 0,
      total_capacity_slots: 10,
      rejected_unfulfilled_sessions: 0,
      available_capacity_slots: 10,
      inferred_demand: 0,
      has_data: true,
    },
    {
      timestamp: '2026-04-19T00:15:00Z',
      devices_total: 3,
      devices_available: 2,
      devices_offline: 0,
      devices_maintenance: 1,
      hosts_total: 1,
      hosts_online: 1,
      active_sessions: 0,
      queued_requests: 0,
      total_capacity_slots: 10,
      rejected_unfulfilled_sessions: 0,
      available_capacity_slots: 10,
      inferred_demand: 0,
      has_data: true,
    },
    {
      timestamp: '2026-04-19T00:30:00Z',
      devices_total: 3,
      devices_available: 3,
      devices_offline: 0,
      devices_maintenance: 0,
      hosts_total: 1,
      hosts_online: 1,
      active_sessions: 1,
      queued_requests: 0,
      total_capacity_slots: 10,
      rejected_unfulfilled_sessions: 0,
      available_capacity_slots: 9,
      inferred_demand: 1,
      has_data: true,
    },
  ],
};

const syntheticGapPoint = {
  timestamp: '2026-04-18T23:45:00Z',
  devices_total: 0,
  devices_available: 0,
  devices_offline: 0,
  devices_maintenance: 0,
  hosts_total: 0,
  hosts_online: 0,
  active_sessions: 0,
  queued_requests: 0,
  total_capacity_slots: 0,
  rejected_unfulfilled_sessions: 0,
  available_capacity_slots: 0,
  inferred_demand: 0,
  has_data: false,
};

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('FleetCapacityStrip', () => {
  beforeEach(() => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: timeline,
      isLoading: false,
    } as ReturnType<typeof useFleetCapacityTimeline>);
  });

  it('renders three labeled sparklines when series is present', async () => {
    wrap(<FleetCapacityStrip />);
    await waitFor(() => {
      expect(screen.getByLabelText(/total devices trend/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/available devices trend/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/active sessions trend/i)).toBeInTheDocument();
    });
  });

  it('renders nothing when fewer than 2 points are available', async () => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: { ...timeline, series: [] },
      isLoading: false,
    } as ReturnType<typeof useFleetCapacityTimeline>);
    const { container } = wrap(<FleetCapacityStrip />);
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it('ignores synthetic gap rows when counting points and rendering totals', async () => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: { ...timeline, series: [syntheticGapPoint, timeline.series[2]!] },
      isLoading: false,
    } as ReturnType<typeof useFleetCapacityTimeline>);
    const { container } = wrap(<FleetCapacityStrip />);
    await waitFor(() => expect(container.firstChild).toBeNull());
  });
});
