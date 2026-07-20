import type { HostTelemetryFreshnessState } from '../lib/hostResourceTelemetry';

const LABELS: Record<HostTelemetryFreshnessState, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  unknown: 'Unknown',
};

const STYLES: Record<HostTelemetryFreshnessState, string> = {
  fresh: 'bg-green-100 text-green-800',
  stale: 'bg-amber-100 text-amber-800',
  unknown: 'bg-slate-100 text-slate-700',
};

type Props = {
  state: HostTelemetryFreshnessState | undefined;
};

export function HostTelemetryStateBadge({ state }: Props) {
  const normalizedState: HostTelemetryFreshnessState = state ?? 'unknown';
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[normalizedState]}`}>
      {LABELS[normalizedState]}
    </span>
  );
}