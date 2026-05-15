import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import FleetHealthHistory from './FleetHealthHistory';
import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';
import type { components } from '../../api/openapi';

vi.mock('../../hooks/useAnalytics', () => ({
  useFleetCapacityTimeline: vi.fn(),
}));

const mockedHook = vi.mocked(useFleetCapacityTimeline);

type SeriesPoint = components['schemas']['FleetCapacityTimelinePoint'];

function makePoint(overrides: Partial<SeriesPoint> = {}): SeriesPoint {
  return {
    timestamp: '2026-04-18T10:00:00Z',
    total_capacity_slots: 0,
    active_sessions: 0,
    queued_requests: 0,
    rejected_unfulfilled_sessions: 0,
    available_capacity_slots: 0,
    inferred_demand: 0,
    hosts_total: 0,
    hosts_online: 0,
    devices_total: 2,
    devices_available: 2,
    devices_offline: 0,
    devices_maintenance: 0,
    has_data: true,
    ...overrides,
  };
}

describe('FleetHealthHistory', () => {
  beforeEach(() => {
    mockedHook.mockReset();
  });

  it('renders the empty state when no buckets have data and no live point is provided', () => {
    mockedHook.mockReturnValue({
      data: {
        date_from: '2026-04-18T08:00:00Z',
        date_to: '2026-04-18T10:00:00Z',
        bucket_minutes: 60,
        series: [
          makePoint({ timestamp: '2026-04-18T08:00:00Z', has_data: false, devices_total: 0, devices_available: 0 }),
          makePoint({ timestamp: '2026-04-18T09:00:00Z', has_data: false, devices_total: 0, devices_available: 0 }),
        ],
      },
    } as unknown as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetHealthHistory />);
    expect(screen.getByText(/not enough history/i)).toBeInTheDocument();
  });

  it('averages over real buckets only and ignores gap buckets', () => {
    mockedHook.mockReturnValue({
      data: {
        date_from: '2026-04-18T08:00:00Z',
        date_to: '2026-04-18T11:00:00Z',
        bucket_minutes: 60,
        series: [
          makePoint({
            timestamp: '2026-04-18T08:00:00Z',
            has_data: true,
            devices_total: 2,
            devices_available: 2,
            devices_offline: 0,
            devices_maintenance: 0,
          }),
          makePoint({
            timestamp: '2026-04-18T09:00:00Z',
            has_data: false,
            devices_total: 0,
            devices_available: 0,
            devices_offline: 0,
            devices_maintenance: 0,
          }),
          makePoint({
            timestamp: '2026-04-18T10:00:00Z',
            has_data: true,
            devices_total: 2,
            devices_available: 1,
            devices_offline: 1,
            devices_maintenance: 0,
          }),
        ],
      },
    } as unknown as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetHealthHistory />);
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText('50')).toBeInTheDocument();
  });

  it('anchors the single-point dot to the right edge of the chart', () => {
    mockedHook.mockReturnValue({
      data: {
        date_from: '2026-04-18T08:00:00Z',
        date_to: '2026-04-18T08:00:00Z',
        bucket_minutes: 60,
        series: [],
      },
    } as unknown as ReturnType<typeof useFleetCapacityTimeline>);

    const { container } = render(
      <FleetHealthHistory livePoint={{ devices_total: 2, devices_offline: 0, devices_maintenance: 0 }} />,
    );
    const circle = container.querySelector('circle');
    expect(circle).not.toBeNull();
    expect(Number(circle!.getAttribute('cx'))).toBeGreaterThan(0);
  });

  it('pins the current value to the live point when provided', () => {
    mockedHook.mockReturnValue({
      data: {
        date_from: '2026-04-18T08:00:00Z',
        date_to: '2026-04-18T09:00:00Z',
        bucket_minutes: 60,
        series: [
          makePoint({
            timestamp: '2026-04-18T08:00:00Z',
            has_data: true,
            devices_total: 2,
            devices_available: 2,
            devices_offline: 0,
            devices_maintenance: 0,
          }),
        ],
      },
    } as unknown as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetHealthHistory livePoint={{ devices_total: 2, devices_offline: 1, devices_maintenance: 0 }} />);
    expect(screen.getByText('50')).toBeInTheDocument();
  });
});
