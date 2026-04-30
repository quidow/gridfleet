import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useDevices } from '../../hooks/useDevices';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { deriveRetriableQueryState } from '../../hooks/useRetriableQueryState';
import PlatformIcon from '../PlatformIcon';
import NoDriverPacksBanner from '../NoDriverPacksBanner';
import Card from '../ui/Card';
import FetchError from '../ui/FetchError';
import ProportionalBar from '../ui/ProportionalBar';
import SectionSkeleton from '../ui/SectionSkeleton';
import FleetHealthHistory from './FleetHealthHistory';
import { deriveDashboardFleetSummary } from './dashboardSummary';
import { resolvePlatformLabel } from '../../lib/labels';

const BAR_SEGMENTS = [
  {
    key: 'available',
    label: 'Available',
    barClassName: 'bg-success-strong',
    dotClassName: 'bg-success-strong',
    to: '/devices?availability_status=available',
  },
  {
    key: 'busy',
    label: 'Busy',
    barClassName: 'bg-warning-strong',
    dotClassName: 'bg-warning-strong',
    to: '/devices?availability_status=busy',
  },
  {
    key: 'reserved',
    label: 'Reserved',
    barClassName: 'bg-info-strong',
    dotClassName: 'bg-info-strong',
    to: '/devices?availability_status=reserved',
  },
  {
    key: 'maintenance',
    label: 'Maintenance',
    barClassName: 'bg-neutral-strong',
    dotClassName: 'bg-neutral-strong',
    to: '/devices?availability_status=maintenance',
  },
  {
    key: 'offline',
    label: 'Offline',
    barClassName: 'bg-danger-strong',
    dotClassName: 'bg-danger-strong',
    to: '/devices?availability_status=offline',
  },
] as const;

type SegmentKey = (typeof BAR_SEGMENTS)[number]['key'];

export default function FleetByPlatformCard() {
  const devicesQuery = useDevices();
  const { data: devices, refetch } = devicesQuery;
  const state = deriveRetriableQueryState(devicesQuery);
  const { data: catalog = [] } = useDriverPackCatalog();
  const enabledPackCount = catalog.filter((pack) => pack.state === 'enabled').length;

  const platformCatalog = useMemo(() => {
    const labels = new Map<string, string>();
    const order = new Map<string, number>();
    let index = 0;
    for (const pack of catalog) {
      for (const platform of pack.platforms ?? []) {
        if (!labels.has(platform.id)) labels.set(platform.id, platform.display_name);
        if (!order.has(platform.id)) order.set(platform.id, index++);
      }
    }
    return { labels, order };
  }, [catalog]);

  const fleet = useMemo(() => deriveDashboardFleetSummary(devices ?? []), [devices]);

  if (state === 'initial-loading') {
    return (
      <Card padding="lg" className="h-full">
        <SectionSkeleton shape="list" rows={4} label="Fleet loading" />
      </Card>
    );
  }

  if (state === 'error') {
    return (
      <Card padding="lg" className="h-full">
        <FetchError message="Could not load fleet data." onRetry={() => void refetch()} />
      </Card>
    );
  }

  const counts: Record<SegmentKey, number> = {
    available: fleet.available,
    busy: fleet.busy,
    offline: fleet.offline,
    maintenance: fleet.maintenance,
    reserved: fleet.reserved,
  };
  const total = fleet.total;
  const platformChips = Object.keys(fleet.platformCounts)
    .filter((platform) => fleet.platformCounts[platform] > 0)
    .sort((a, b) => {
      const aOrder = platformCatalog.order.get(a);
      const bOrder = platformCatalog.order.get(b);
      if (aOrder !== undefined && bOrder !== undefined) return aOrder - bOrder;
      if (aOrder !== undefined) return -1;
      if (bOrder !== undefined) return 1;
      return a.localeCompare(b);
    });
  const attentionLink = fleet.needsAttention > 0
    ? {
        key: 'needs_attention',
        label: 'Needs attention',
        count: fleet.needsAttention,
        to: '/devices?needs_attention=true',
        dotClass: 'bg-danger-strong',
      }
    : null;

  return (
    <Card padding="lg" className="flex h-full flex-col">
      <div className="flex items-baseline justify-between">
        <h2 className="heading-section">Fleet</h2>
        <span className="font-mono tabular-nums text-sm text-text-2">
          {total} device{total !== 1 ? 's' : ''}
        </span>
      </div>

      {enabledPackCount === 0 && (
        <div className="mt-3">
          <NoDriverPacksBanner packCount={enabledPackCount} />
        </div>
      )}

      {total === 0 ? (
        <p className="mt-4 text-sm text-text-2">No devices registered.</p>
      ) : (
        <>
          <ProportionalBar
            segments={BAR_SEGMENTS.map((segment) => ({
              ...segment,
              count: counts[segment.key],
            }))}
          />

          {attentionLink ? (
            <div className="mt-4 flex flex-wrap gap-2 border-t border-border pt-4">
              <Link
                to={attentionLink.to}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-xs font-medium text-text-2 transition-colors hover:border-border-strong hover:bg-surface-1"
              >
                <span className={`inline-block h-1.5 w-1.5 rounded-full ${attentionLink.dotClass}`} />
                <span>{attentionLink.label}</span>
                <span className="font-mono tabular-nums text-text-1">{attentionLink.count}</span>
              </Link>
            </div>
          ) : null}

          {platformChips.length > 0 && (
            <>
              <div className="mt-4 border-t border-border pt-4" />
              <div className="flex flex-wrap gap-2">
                {platformChips.map((platform) => {
                  const label = resolvePlatformLabel(platform, platformCatalog.labels.get(platform));
                  return (
                    <Link
                      key={platform}
                      to={`/devices?platform_id=${platform}`}
                      className="inline-flex items-center gap-2 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-xs font-medium text-text-1 hover:border-border-strong hover:bg-surface-1 transition-colors"
                      aria-label={`${label} — ${fleet.platformCounts[platform]} devices`}
                    >
                      <PlatformIcon platformId={platform} platformLabel={label} showLabel />
                      <span className="font-mono tabular-nums text-text-2">{fleet.platformCounts[platform]}</span>
                    </Link>
                  );
                })}
              </div>
            </>
          )}

          <FleetHealthHistory
            livePoint={{
              devices_total: fleet.total,
              devices_offline: fleet.offline,
              devices_maintenance: fleet.maintenance,
            }}
          />
        </>
      )}
    </Card>
  );
}
