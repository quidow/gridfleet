import type { ReactNode } from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Analytics from './Analytics';

const analyticsHooks = vi.hoisted(() => ({
  useSessionSummary: vi.fn(),
  useDeviceUtilization: vi.fn(),
  useDeviceReliability: vi.fn(),
  useFleetCapacityTimeline: vi.fn(),
}));

vi.mock('../hooks/useAnalytics', () => ({
  useSessionSummary: analyticsHooks.useSessionSummary,
  useDeviceUtilization: analyticsHooks.useDeviceUtilization,
  useDeviceReliability: analyticsHooks.useDeviceReliability,
  useFleetCapacityTimeline: analyticsHooks.useFleetCapacityTimeline,
}));

vi.mock('../hooks/usePageTitle', () => ({
  usePageTitle: vi.fn(),
}));

vi.mock('../hooks/useDevRenderCrashTrigger', () => ({
  useDevRenderCrashTrigger: vi.fn(),
}));

vi.mock('../components/analytics/DateRangePicker', () => ({
  default: () => <div>Date Range</div>,
}));

vi.mock('../components/analytics/SessionTrendsTab', () => ({
  default: () => <div>Session Trends</div>,
}));

vi.mock('../components/analytics/DeviceUtilizationTab', () => ({
  default: () => <div>Device Utilization</div>,
}));

vi.mock('../components/analytics/ReliabilityTab', () => ({
  default: () => <div>Reliability</div>,
}));

vi.mock('../components/analytics/FleetCapacityTab', () => ({
  default: () => <div>Fleet Capacity</div>,
}));

vi.mock('../components/ErrorBoundary', () => ({
  SectionErrorBoundary: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock('../components/ui/Tabs', () => ({
  default: () => <div>Tabs</div>,
}));

describe('Analytics', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-22T12:00:00Z'));

    analyticsHooks.useSessionSummary.mockImplementation((params?: { group_by?: string }) => {
      if (params?.group_by === 'day') return { dataUpdatedAt: Date.now() - 45_000 };
      if (params?.group_by === 'platform') return { dataUpdatedAt: Date.now() - 30_000 };
      return { dataUpdatedAt: 0 };
    });
    analyticsHooks.useDeviceUtilization.mockReturnValue({ dataUpdatedAt: 0 });
    analyticsHooks.useDeviceReliability.mockReturnValue({ dataUpdatedAt: 0 });
    analyticsHooks.useFleetCapacityTimeline.mockReturnValue({ dataUpdatedAt: 0 });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('shows analytics subtitle with freshness based on active tab data', () => {
    render(
      <MemoryRouter initialEntries={['/analytics']}>
        <Analytics />
      </MemoryRouter>,
    );

    expect(screen.getByRole('heading', { name: 'Analytics' })).toBeInTheDocument();
    expect(
      screen.getByText('Fleet throughput, reliability, and capacity · updated 30s ago'),
    ).toBeInTheDocument();
  });
});
