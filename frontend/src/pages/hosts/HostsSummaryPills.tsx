import { Link } from 'react-router-dom';
import SummaryPill from '../../components/ui/SummaryPill';
import type { HostsFleetStats } from './hostsSummary';
import { buildHostsSummaryHref } from './hostsSummary';

type Props = {
  stats: HostsFleetStats;
  searchParams: URLSearchParams;
  isLoading: boolean;
  disabled?: boolean;
};

function SummaryItem({
  label,
  value,
  tone,
  to,
}: {
  label: string;
  value: number | string;
  tone: 'ok' | 'warn' | 'error' | 'neutral';
  to?: string;
}) {
  const pill = <SummaryPill tone={tone} label={label} value={value} />;

  if (!to) {
    return pill;
  }

  return (
    <Link
      to={to}
      aria-label={`${label} ${value}`}
      className="rounded-full transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2"
    >
      {pill}
    </Link>
  );
}

function displayValue(isLoading: boolean, disabled: boolean | undefined, value: number) {
  return isLoading || disabled ? '—' : value;
}

export default function HostsSummaryPills({
  stats,
  searchParams,
  isLoading,
  disabled = false,
}: Props) {
  const interactive = !isLoading && !disabled;

  return (
    <>
      <SummaryItem
        label="Total"
        value={displayValue(isLoading, disabled, stats.total)}
        tone="neutral"
        to={interactive ? buildHostsSummaryHref(searchParams) : undefined}
      />
      <SummaryItem
        label="Online"
        value={displayValue(isLoading, disabled, stats.online)}
        tone={interactive && stats.online > 0 ? 'ok' : 'neutral'}
        to={interactive ? buildHostsSummaryHref(searchParams, { status: 'online' }) : undefined}
      />
      <SummaryItem
        label="Offline"
        value={displayValue(isLoading, disabled, stats.offline)}
        tone={interactive && stats.offline > 0 ? 'error' : 'neutral'}
        to={interactive ? buildHostsSummaryHref(searchParams, { status: 'offline' }) : undefined}
      />
      <SummaryItem
        label="Stale agents"
        value={displayValue(isLoading, disabled, stats.staleAgents)}
        tone={interactive && stats.staleAgents > 0 ? 'warn' : 'neutral'}
        to={interactive ? buildHostsSummaryHref(searchParams, { agentVersionStatus: 'outdated' }) : undefined}
      />
    </>
  );
}
