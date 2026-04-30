import { useSearchParams } from 'react-router-dom';
import { useMemo } from 'react';
import DateRangePicker from '../components/analytics/DateRangePicker';
import type { Preset } from '../components/analytics/DateRangePicker';
import SessionTrendsTab from '../components/analytics/SessionTrendsTab';
import DeviceUtilizationTab from '../components/analytics/DeviceUtilizationTab';
import ReliabilityTab from '../components/analytics/ReliabilityTab';
import FleetCapacityTab from '../components/analytics/FleetCapacityTab';
import {
  useDeviceReliability,
  useDeviceUtilization,
  useFleetCapacityTimeline,
  useSessionSummary,
} from '../hooks/useAnalytics';
import { usePageTitle } from '../hooks/usePageTitle';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { useDevRenderCrashTrigger } from '../hooks/useDevRenderCrashTrigger';
import PageHeader from '../components/ui/PageHeader';
import Tabs from '../components/ui/Tabs';

type Tab = 'sessions' | 'utilization' | 'reliability' | 'fleet-capacity';
const DEFAULT_PRESET: Preset = '7d';
const FLEET_CAPACITY_DEFAULT_PRESET: Preset = '24h';

const TABS: { key: Tab; label: string }[] = [
  { key: 'sessions', label: 'Session Trends' },
  { key: 'utilization', label: 'Device Utilization' },
  { key: 'reliability', label: 'Reliability' },
  { key: 'fleet-capacity', label: 'Fleet Capacity' },
];

function defaultDateFrom(preset: Preset): string {
  const d = new Date();
  if (preset === '24h') d.setDate(d.getDate() - 1);
  else if (preset === '30d') d.setDate(d.getDate() - 30);
  else d.setDate(d.getDate() - 7);
  return d.toISOString();
}

function isTab(value: string | null): value is Tab {
  return TABS.some((item) => item.key === value);
}

export default function Analytics() {
  useDevRenderCrashTrigger('analytics-page');
  usePageTitle('Analytics');
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get('tab');
  const tab = isTab(requestedTab) ? requestedTab : 'sessions';
  const defaultPreset = tab === 'fleet-capacity' ? FLEET_CAPACITY_DEFAULT_PRESET : DEFAULT_PRESET;
  const defaultRange = useMemo(
    () => ({ dateFrom: defaultDateFrom(defaultPreset), dateTo: new Date().toISOString() }),
    [defaultPreset],
  );

  const dateFrom = searchParams.get('date_from') || defaultRange.dateFrom;
  const dateTo = searchParams.get('date_to') || defaultRange.dateTo;
  const hasExplicitDates = searchParams.has('date_from') || searchParams.has('date_to');
  const preset = (searchParams.get('preset') as Preset) || (hasExplicitDates ? 'custom' : defaultPreset);

  const params = useMemo(() => ({ date_from: dateFrom, date_to: dateTo }), [dateFrom, dateTo]);
  const fleetCapacityParams = useMemo(() => ({ ...params, bucket_minutes: 1 }), [params]);
  const sessionsDayQuery = useSessionSummary({ ...params, group_by: 'day' }, { enabled: tab === 'sessions' });
  const sessionsPlatformQuery = useSessionSummary({ ...params, group_by: 'platform' }, { enabled: tab === 'sessions' });
  const utilizationQuery = useDeviceUtilization(params, { enabled: tab === 'utilization' });
  const reliabilityQuery = useDeviceReliability(params, { enabled: tab === 'reliability' });
  const capacityQuery = useFleetCapacityTimeline(fleetCapacityParams, { enabled: tab === 'fleet-capacity' });

  const dataUpdatedAt =
    tab === 'sessions'
      ? Math.max(
        sessionsDayQuery.dataUpdatedAt ?? 0,
        sessionsPlatformQuery.dataUpdatedAt ?? 0,
      )
      : tab === 'utilization'
        ? utilizationQuery.dataUpdatedAt ?? 0
        : tab === 'reliability'
          ? reliabilityQuery.dataUpdatedAt ?? 0
          : capacityQuery.dataUpdatedAt ?? 0;

  function setTab(t: Tab) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('tab', t);
      return next;
    });
  }

  function setDateRange(from: string, to: string, p: Preset) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (from) next.set('date_from', from);
      else next.delete('date_from');
      if (to) next.set('date_to', to);
      else next.delete('date_to');
      next.set('preset', p);
      return next;
    });
  }

  return (
    <div>
      <PageHeader
        title="Analytics"
        subtitle="Fleet throughput, reliability, and capacity"
        updatedAt={dataUpdatedAt}
      />

      <div className="fade-in-stagger flex flex-col gap-6">
      <div>
        <DateRangePicker dateFrom={dateFrom} dateTo={dateTo} activePreset={preset} onChange={setDateRange} />
      </div>

      <Tabs
        tabs={TABS.map((t) => ({ id: t.key, label: t.label }))}
        activeId={tab}
        onChange={(id) => setTab(id as Tab)}
      />

      {tab === 'sessions' && (
        <SectionErrorBoundary resetKey={tab} scope="analytics-session-trends">
          <SessionTrendsTab params={params} />
        </SectionErrorBoundary>
      )}
      {tab === 'utilization' && (
        <SectionErrorBoundary resetKey={tab} scope="analytics-device-utilization">
          <DeviceUtilizationTab params={params} />
        </SectionErrorBoundary>
      )}
      {tab === 'reliability' && (
        <SectionErrorBoundary resetKey={tab} scope="analytics-reliability">
          <ReliabilityTab params={params} />
        </SectionErrorBoundary>
      )}
      {tab === 'fleet-capacity' && (
        <SectionErrorBoundary resetKey={tab} scope="analytics-fleet-capacity">
          <FleetCapacityTab params={params} />
        </SectionErrorBoundary>
      )}
      </div>
    </div>
  );
}
