import { Link } from 'react-router-dom';
import { useDevices } from '../../hooks/useDevices';
import { useRuns } from '../../hooks/useRuns';
import { PlatformIcon } from '../PlatformIcon';
import { StatusBadge } from '../StatusBadge';
import { Badge } from '../ui/Badge';
import { Card } from '../ui/Card';
import { SectionSkeleton } from '../ui/SectionSkeleton';
import { deviceChipStatus } from '../../lib/deviceState';
import { DEVICE_STATUS_LABELS } from '../../lib/labels';
import { formatRelativeTime } from '../../utils/dateFormatting';
import type { DeviceChipStatus, DeviceRead, RunRead } from '../../types';
import { deriveDashboardFleetSummary, isActiveRun } from './dashboardSummary';

const MAX_RUNS = 5;
const MAX_BUSY = 6;

function runItems(payload: unknown): RunRead[] {
  if (Array.isArray(payload)) return payload as RunRead[];
  if (payload && typeof payload === 'object' && 'items' in payload) {
    const items = (payload as { items?: unknown }).items;
    return Array.isArray(items) ? (items as RunRead[]) : [];
  }
  return [];
}

function availabilityTone(status: DeviceChipStatus) {
  switch (status) {
    case 'available': return 'success' as const;
    case 'busy': return 'warning' as const;
    case 'verifying': return 'warning' as const;
    case 'offline': return 'critical' as const;
    case 'maintenance': return 'neutral' as const;
  }
}

function SectionHead({ label, empty }: { label: string; empty: boolean }) {
  return (
    <h3 className="heading-label" aria-label={empty ? `${label} · none` : label}>
      {label}
      {empty ? <span className="font-normal normal-case text-text-3">{' · none'}</span> : null}
    </h3>
  );
}

function DeviceStateRow({
  deviceId,
  fallbackName,
  platformId,
  platformLabel,
  live,
}: {
  deviceId: string;
  fallbackName: string;
  platformId: string;
  platformLabel: string | null;
  live: DeviceRead | undefined;
}) {
  const status = live ? deviceChipStatus(live) : null;
  return (
    <li className="flex items-center gap-3 py-1.5 text-sm">
      <Link
        to={`/devices/${deviceId}`}
        className="min-w-0 truncate font-medium text-accent hover:text-accent-hover"
      >
        {live?.name ?? fallbackName}
      </Link>
      <span className="flex shrink-0 items-center gap-3">
        <PlatformIcon platformId={live?.platform_id ?? platformId} platformLabel={live?.platform_label ?? platformLabel} />
        {status ? <Badge tone={availabilityTone(status)}>{DEVICE_STATUS_LABELS[status]}</Badge> : null}
      </span>
    </li>
  );
}

function RunGroup({ run, deviceById }: { run: RunRead; deviceById: Map<string, DeviceRead> }) {
  const reserved = run.reserved_devices ?? [];
  const startedAt = run.started_at ?? run.created_at;
  return (
    <li className="py-3 first:pt-0 last:pb-0">
      <div className="flex items-center justify-between gap-3 text-sm">
        <div className="flex min-w-0 items-center gap-3">
          <Link to={`/runs/${run.id}`} className="truncate font-medium text-accent hover:text-accent-hover">
            {run.name}
          </Link>
          <StatusBadge status={run.state} />
        </div>
        <p className="shrink-0 text-xs text-text-2">
          <span className="font-mono tabular-nums">{reserved.length}</span> device{reserved.length === 1 ? '' : 's'}
          {startedAt ? <span className="before:mx-1.5 before:content-['·']">{formatRelativeTime(startedAt)}</span> : null}
        </p>
      </div>
      {reserved.length > 0 ? (
        <ul className="mt-1.5 grid grid-cols-1 gap-x-8 border-l-2 border-border pl-4 sm:grid-cols-2 xl:grid-cols-3">
          {reserved.map((info) => (
            <DeviceStateRow
              key={info.device_id}
              deviceId={info.device_id}
              fallbackName={info.name ?? info.identity_value}
              platformId={info.platform_id}
              platformLabel={info.platform_label ?? null}
              live={deviceById.get(info.device_id)}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ActivityCard() {
  const runsQuery = useRuns();
  const devicesQuery = useDevices();

  if (runsQuery.status === 'pending' || devicesQuery.status === 'pending') {
    return (
      <Card padding="lg">
        <SectionSkeleton shape="split" rows={2} label="Activity loading" />
      </Card>
    );
  }

  const devices = devicesQuery.data ?? [];
  const deviceById = new Map(devices.map((device) => [device.id, device]));
  const activeRuns = runItems(runsQuery.data).filter(isActiveRun);
  const visibleRuns = activeRuns.slice(0, MAX_RUNS);
  const runOverflow = activeRuns.length - visibleRuns.length;
  const strayBusy = deriveDashboardFleetSummary(devices).busyDevices.filter((device) => !device.is_reserved);

  return (
    <Card padding="lg" as="section">
      <h2 className="heading-section">Activity</h2>

      <div className="mt-4">
        <SectionHead label="Active runs" empty={activeRuns.length === 0} />
        {visibleRuns.length > 0 ? (
          <ul className="mt-1 flex flex-col divide-y divide-border">
            {visibleRuns.map((run) => (
              <RunGroup key={run.id} run={run} deviceById={deviceById} />
            ))}
          </ul>
        ) : null}
        {runOverflow > 0 ? (
          <Link to="/runs" className="mt-2 block text-xs font-medium text-text-2 transition-colors hover:text-accent">
            + {runOverflow} more
          </Link>
        ) : null}
      </div>

      <div className="mt-4 border-t border-border pt-4">
        <SectionHead label="Busy outside runs" empty={strayBusy.length === 0} />
        {strayBusy.length > 0 ? (
          <ul className="mt-1 grid grid-cols-1 gap-x-8 sm:grid-cols-2 xl:grid-cols-3">
            {strayBusy.slice(0, MAX_BUSY).map((device) => (
              <DeviceStateRow
                key={device.id}
                deviceId={device.id}
                fallbackName={device.name}
                platformId={device.platform_id}
                platformLabel={device.platform_label}
                live={device}
              />
            ))}
          </ul>
        ) : null}
      </div>
    </Card>
  );
}
