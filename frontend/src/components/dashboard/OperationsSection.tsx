import { useMemo } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, BarChart3, TrendingUp } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useFleetOverview } from '../../hooks/useAnalytics';
import { useDevices } from '../../hooks/useDevices';
import { useRuns } from '../../hooks/useRuns';
import { deriveRetriableQueryState } from '../../hooks/useRetriableQueryState';
import Sparkline from '../ui/Sparkline';
import { useSessionsDaily } from '../../hooks/useSessionsDaily';
import PlatformIcon from '../PlatformIcon';
import StatusBadge from '../StatusBadge';
import Badge from '../ui/Badge';
import { deviceChipStatus } from '../../lib/deviceState';
import { DEVICE_STATUS_LABELS } from '../../lib/labels';
import Card from '../ui/Card';
import FetchError from '../ui/FetchError';
import SectionSkeleton from '../ui/SectionSkeleton';
import { formatRelativeTime } from '../../utils/dateFormatting';
import type { DeviceChipStatus, DeviceRead, RunRead } from '../../types';
import { deriveDashboardFleetSummary, isActiveRun } from './dashboardSummary';

function last7DaysParams() {
  const now = new Date();
  const from = new Date(now);
  from.setDate(from.getDate() - 7);
  return { date_from: from.toISOString(), date_to: now.toISOString() };
}

function runItems(payload: unknown): RunRead[] {
  if (Array.isArray(payload)) return payload as RunRead[];
  if (payload && typeof payload === 'object' && 'items' in payload) {
    const items = (payload as { items?: unknown }).items;
    return Array.isArray(items) ? (items as RunRead[]) : [];
  }
  return [];
}

function IdleCell({ title }: { title: string }) {
  return (
    <p className="rounded-md border border-dashed border-border bg-surface-2 px-3 py-3 text-sm text-text-2">
      {title}
    </p>
  );
}

function ActiveRunsList({ runs }: { runs: RunRead[] }) {
  return (
    <ul className="divide-y divide-border rounded-lg border border-border bg-surface-1">
      {runs.slice(0, 5).map((run) => {
        const deviceCount = run.reserved_devices?.length ?? 0;
        const startedAt = run.started_at ?? run.created_at;
        return (
          <li key={run.id} className="flex items-center justify-between gap-3 px-3 py-2.5 text-sm">
            <div className="min-w-0 flex-1">
              <Link to={`/runs/${run.id}`} className="block truncate font-medium text-accent hover:text-accent-hover">
                {run.name}
              </Link>
              <p className="mt-0.5 text-xs text-text-2">
                <span className="font-mono tabular-nums">{deviceCount}</span> device{deviceCount === 1 ? '' : 's'}
                {startedAt ? <span className="before:mx-1.5 before:content-['·']">{formatRelativeTime(startedAt)}</span> : null}
              </p>
            </div>
            <div className="shrink-0">
              <StatusBadge status={run.state} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function availabilityTone(status: DeviceChipStatus) {
  switch (status) {
    case 'available': return 'success' as const;
    case 'busy': return 'warning' as const;
    case 'verifying': return 'warning' as const;
    case 'offline': return 'danger' as const;
    case 'maintenance': return 'neutral' as const;
    case 'reserved': return 'info' as const;
  }
}

function BusyDevicesList({ devices }: { devices: DeviceRead[] }) {
  return (
    <ul className="divide-y divide-border rounded-lg border border-border bg-surface-1">
      {devices.slice(0, 6).map((device) => (
        (() => {
          const status = deviceChipStatus(device);
          return (
            <li key={device.id} className="grid grid-cols-[minmax(0,1fr),7rem,auto] items-center gap-3 px-3 py-2.5 text-sm">
              <Link to={`/devices/${device.id}`} className="min-w-0 truncate font-medium text-accent hover:text-accent-hover">
                {device.name}
              </Link>
              <PlatformIcon platformId={device.platform_id} platformLabel={device.platform_label} />
              <Badge tone={availabilityTone(status)}>
                {DEVICE_STATUS_LABELS[status]}
              </Badge>
            </li>
          );
        })()
      ))}
    </ul>
  );
}

const NUMERIC_VALUE_RE = /^-?\d+(?:\.\d+)?%?$/;

function isNumericValue(value: string | number): boolean {
  if (typeof value === 'number') return Number.isFinite(value);
  return NUMERIC_VALUE_RE.test(value.trim());
}

export function MetricTile({
  icon: Icon,
  label,
  value,
  to,
  tone,
  sparkline,
}: {
  icon: LucideIcon;
  label: string;
  value: string | number;
  to: string;
  tone: 'neutral' | 'success' | 'warning' | 'info';
  sparkline?: ReactNode;
}) {
  const iconClass = {
    neutral: 'text-text-3',
    success: 'text-success-strong',
    warning: 'text-warning-strong',
    info: 'text-info-strong',
  }[tone];

  const numeric = isNumericValue(value);
  const valueClass = numeric
    ? 'metric-numeric mt-1 font-mono text-lg font-semibold tabular-nums text-text-1'
    : 'mt-1 text-lg font-semibold text-text-1';

  return (
    <Link to={to} className="hover-lift block rounded-md border border-border bg-surface-1 px-3 py-2 hover:border-border-strong">
      <div className="flex items-center gap-2 text-xs text-text-2">
        <Icon size={14} className={iconClass} />
        <span>{label}</span>
      </div>
      <div className="flex items-end justify-between gap-3">
        <p className={valueClass}>{value}</p>
        {sparkline ? <span className={`${iconClass} opacity-80`}>{sparkline}</span> : null}
      </div>
    </Link>
  );
}

export default function OperationsSection() {
  const runsQuery = useRuns();
  const devicesQuery = useDevices();
  const fleetOverviewParams = useMemo(() => last7DaysParams(), []);
  const analyticsQuery = useFleetOverview(fleetOverviewParams);
  const { series: sessionsDaily } = useSessionsDaily(7);
  const passRateSeries = useMemo(
    () =>
      sessionsDaily
        .filter((d) => d.total > 0)
        .map((d) => (d.passed / d.total) * 100),
    [sessionsDaily],
  );
  const hasPassRateTrend = passRateSeries.length >= 2;

  const runsState = deriveRetriableQueryState(runsQuery);
  const devicesState = deriveRetriableQueryState(devicesQuery);
  const activeRuns = useMemo(() => runItems(runsQuery.data).filter(isActiveRun), [runsQuery.data]);
  const busyDevices = useMemo(
    () => deriveDashboardFleetSummary(devicesQuery.data ?? []).busyDevices,
    [devicesQuery.data],
  );

  const isLoading = runsState === 'initial-loading' || devicesState === 'initial-loading';
  const runsError = runsState === 'error';
  const devicesError = devicesState === 'error';

  return (
    <Card padding="none" as="section">
      <div className="px-6 py-5">
        <h2 className="heading-section">Operations</h2>
        <p className="mt-1 text-xs text-text-2">Runs, reservations, and fleet usage.</p>
      </div>

      {isLoading ? (
        <div className="border-t border-border p-6">
          <SectionSkeleton shape="split" rows={3} label="Operations loading" />
        </div>
      ) : runsError || devicesError ? (
        <div className="space-y-3 border-t border-border p-6">
          {runsError ? <FetchError message="Could not load active runs." onRetry={() => void runsQuery.refetch()} /> : null}
          {devicesError ? <FetchError message="Could not load busy devices." onRetry={() => void devicesQuery.refetch()} /> : null}
        </div>
      ) : (
        <div className="grid grid-cols-1 divide-y divide-border border-t border-border lg:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1fr)_minmax(0,1fr)] lg:divide-x lg:divide-y-0">
          <div className="p-5">
            <div className="mb-3">
              <h3 className="heading-label">Last 7 days</h3>
            </div>
            {analyticsQuery.isError ? (
              <FetchError message="Could not load analytics summary." onRetry={() => void analyticsQuery.refetch()} />
            ) : (
              <div className="grid grid-cols-1 gap-3">
                <MetricTile
                  icon={TrendingUp}
                  label="Pass rate"
                  value={analyticsQuery.data?.pass_rate_pct != null ? `${analyticsQuery.data.pass_rate_pct}%` : 'No runs'}
                  to="/analytics"
                  tone={analyticsQuery.data?.pass_rate_pct != null ? 'success' : 'neutral'}
                  sparkline={
                    hasPassRateTrend ? (
                      <Sparkline
                        values={passRateSeries}
                        width={60}
                        height={16}
                        className="text-success-strong"
                        ariaLabel={`Pass rate last 7 days: ${passRateSeries.map((v) => `${v.toFixed(0)}%`).join(', ')}`}
                      />
                    ) : undefined
                  }
                />
                <MetricTile
                  icon={BarChart3}
                  label="Fleet utilization"
                  value={analyticsQuery.data?.avg_utilization_pct != null ? `${analyticsQuery.data.avg_utilization_pct}%` : '—'}
                  to="/analytics"
                  tone={analyticsQuery.data?.avg_utilization_pct != null ? 'info' : 'neutral'}
                />
                <MetricTile
                  icon={AlertTriangle}
                  label="Reliability watchlist"
                  value={analyticsQuery.data?.devices_needing_attention ?? '—'}
                  to="/analytics?tab=reliability"
                  tone={(analyticsQuery.data?.devices_needing_attention ?? 0) > 0 ? 'warning' : 'neutral'}
                />
              </div>
            )}
          </div>

          <div className="p-5">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="heading-label">Active runs</h3>
              <Link to="/runs" className="text-xs font-medium text-accent hover:text-accent-hover">View runs</Link>
            </div>
            {activeRuns.length === 0 ? (
              <IdleCell title="No active runs." />
            ) : (
              <ActiveRunsList runs={activeRuns} />
            )}
          </div>

          <div className="p-5">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="heading-label">Busy devices</h3>
              <Link to="/devices?status=busy" className="text-xs font-medium text-accent hover:text-accent-hover">View busy</Link>
            </div>
            {busyDevices.length === 0 ? (
              <IdleCell title="No busy devices." />
            ) : (
              <BusyDevicesList devices={busyDevices} />
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
