import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import FleetCapacityTab from './FleetCapacityTab';
import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';
import type { FleetCapacityTimeline } from '../../types';
import { buildFleetCapacityChartData } from '../../lib/fleetCapacityTimeline';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: ({ name }: { name: string }) => <div data-testid="line">{name}</div>,
  XAxis: () => <div data-testid="x-axis" />,
  YAxis: () => <div data-testid="y-axis" />,
  CartesianGrid: () => <div data-testid="grid" />,
  Tooltip: () => <div data-testid="tooltip" />,
  Legend: () => <div data-testid="legend" />,
}));

vi.mock('../../hooks/useAnalytics', () => ({
  useFleetCapacityTimeline: vi.fn(),
}));

const mockedUseFleetCapacityTimeline = vi.mocked(useFleetCapacityTimeline);

function makeTimeline(overrides: Partial<FleetCapacityTimeline> = {}): FleetCapacityTimeline {
  return {
    date_from: '2026-04-18T10:00:00Z',
    date_to: '2026-04-18T10:05:00Z',
    bucket_minutes: 1,
    series: [
      {
        timestamp: '2026-04-18T10:00:00Z',
        total_capacity_slots: 4,
        active_sessions: 2,
        queued_requests: 0,
        rejected_unfulfilled_sessions: 0,
        available_capacity_slots: 2,
        inferred_demand: 2,
        hosts_total: 2,
        hosts_online: 2,
        devices_total: 4,
        devices_available: 2,
        devices_offline: 1,
        devices_maintenance: 0,
        has_data: true,
      },
      {
        timestamp: '2026-04-18T10:01:00Z',
        total_capacity_slots: 4,
        active_sessions: 4,
        queued_requests: 2,
        rejected_unfulfilled_sessions: 1,
        available_capacity_slots: 0,
        inferred_demand: 7,
        hosts_total: 2,
        hosts_online: 2,
        devices_total: 4,
        devices_available: 0,
        devices_offline: 2,
        devices_maintenance: 1,
        has_data: true,
      },
    ],
    ...overrides,
  };
}

describe('buildFleetCapacityChartData', () => {
  it('emits gap rows for buckets where has_data is false', () => {
    const baseline = makeTimeline();
    const chartData = buildFleetCapacityChartData({
      ...baseline,
      series: [
        baseline.series[0]!,
        {
          ...baseline.series[1]!,
          timestamp: '2026-04-18T10:01:00Z',
          total_capacity_slots: 0,
          active_sessions: 0,
          queued_requests: 0,
          rejected_unfulfilled_sessions: 0,
          available_capacity_slots: 0,
          inferred_demand: 0,
          hosts_total: 0,
          hosts_online: 0,
          devices_total: 0,
          devices_available: 0,
          devices_offline: 0,
          devices_maintenance: 0,
          has_data: false,
        },
        {
          ...baseline.series[1]!,
          timestamp: '2026-04-18T10:02:00Z',
          has_data: true,
        },
      ],
    });

    expect(chartData.map((row) => row.isGap)).toEqual([false, true, false]);
    expect(chartData[1]).toMatchObject({
      isGap: true,
      total_capacity_slots: null,
      active_sessions: null,
      queued_requests: null,
      rejected_unfulfilled_sessions: null,
      available_capacity_slots: null,
      inferred_demand: null,
    });
  });
});

describe('FleetCapacityTab', () => {
  it('renders fleet capacity lines and pressure totals', () => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: makeTimeline(),
      isLoading: false,
    } as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetCapacityTab params={{ date_from: '2026-04-18T10:00:00Z', date_to: '2026-04-18T10:05:00Z' }} />);

    expect(screen.getByText('Fleet Capacity')).toBeInTheDocument();
    expect(screen.getAllByText('Supply').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Active usage').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Queued requests').length).toBeGreaterThan(0);
    expect(screen.getByText('Unfulfilled attempts')).toBeInTheDocument();
    expect(screen.getByText('Peak inferred demand')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
  });

  it('renders empty state when no snapshots exist', () => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: makeTimeline({ series: [] }),
      isLoading: false,
    } as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetCapacityTab params={{}} />);

    expect(screen.getByText('No capacity snapshots yet')).toBeInTheDocument();
  });

  it('renders loading skeleton while fetching', () => {
    mockedUseFleetCapacityTimeline.mockReturnValue({
      data: undefined,
      isLoading: true,
    } as ReturnType<typeof useFleetCapacityTimeline>);

    render(<FleetCapacityTab params={{}} />);

    expect(screen.getByRole('status', { name: 'Fleet capacity loading' })).toBeInTheDocument();
  });
});
