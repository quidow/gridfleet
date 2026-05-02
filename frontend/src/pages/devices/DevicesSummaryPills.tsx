import { Link } from 'react-router-dom';
import SummaryPill from '../../components/ui/SummaryPill';
import {
  buildDevicesSummaryHref,
  getAttentionHrefOptions,
  getAttentionTone,
  type DevicesSummaryStats,
} from './devicesSummary';

type Props = {
  stats: DevicesSummaryStats;
  searchParams: URLSearchParams;
  isLoading: boolean;
};

function SummaryLink({
  label,
  value,
  tone,
  to,
}: {
  label: string;
  value: number | string;
  tone: 'ok' | 'warn' | 'error' | 'neutral';
  to: string;
}) {
  return (
    <Link
      to={to}
      aria-label={`${label} ${value}`}
      className="rounded-full transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-surface-0"
    >
      <SummaryPill tone={tone} label={label} value={value} />
    </Link>
  );
}

function displayValue(isLoading: boolean, value: number) {
  return isLoading ? '—' : value;
}

export default function DevicesSummaryPills({ stats, searchParams, isLoading }: Props) {
  const attentionHref = buildDevicesSummaryHref(searchParams, getAttentionHrefOptions());

  return (
    <>
      <SummaryLink
        label="Available"
        value={displayValue(isLoading, stats.available)}
        tone={isLoading ? 'neutral' : 'ok'}
        to={buildDevicesSummaryHref(searchParams, { availabilityStatus: 'available' })}
      />
      <SummaryLink
        label="Busy"
        value={displayValue(isLoading, stats.busy)}
        tone={isLoading ? 'neutral' : 'warn'}
        to={buildDevicesSummaryHref(searchParams, { availabilityStatus: 'busy' })}
      />
      {stats.reserved > 0 ? (
        <SummaryLink
          label="Reserved"
          value={displayValue(isLoading, stats.reserved)}
          tone="neutral"
          to={buildDevicesSummaryHref(searchParams, { availabilityStatus: 'reserved' })}
        />
      ) : null}
      <SummaryLink
        label="Offline"
        value={displayValue(isLoading, stats.offline)}
        tone={isLoading ? 'neutral' : 'error'}
        to={buildDevicesSummaryHref(searchParams, { availabilityStatus: 'offline' })}
      />
      {stats.maintenance > 0 ? (
        <SummaryLink
          label="Maintenance"
          value={displayValue(isLoading, stats.maintenance)}
          tone="neutral"
          to={buildDevicesSummaryHref(searchParams, { availabilityStatus: 'maintenance' })}
        />
      ) : null}
      <SummaryLink
        label="Needs attention"
        value={displayValue(isLoading, stats.attentionCount)}
        tone={isLoading ? 'neutral' : getAttentionTone(stats)}
        to={attentionHref}
      />
    </>
  );
}
