import type { HostTelemetryFreshnessState } from '../lib/hostResourceTelemetry';

const LABELS: Record<HostTelemetryFreshnessState, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  unknown: 'Unknown',
};

const STYLES: Record<HostTelemetryFreshnessState, string> = {
  fresh: 'bg-info-soft text-info-foreground',
  stale: 'bg-warning-soft text-warning-foreground',
  unknown: 'bg-neutral-soft text-neutral-foreground',
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