import { Badge } from './ui/Badge';
import type { BadgeTone } from './ui/Badge';
import type { HostTelemetryFreshnessState } from '../lib/hostResourceTelemetry';

const LABELS: Record<HostTelemetryFreshnessState, string> = {
  fresh: 'Fresh',
  stale: 'Stale',
  unknown: 'Unknown',
};

const TONES: Record<HostTelemetryFreshnessState, BadgeTone> = {
  fresh: 'info',
  stale: 'warning',
  unknown: 'neutral',
};

type Props = {
  state: HostTelemetryFreshnessState | undefined;
};

export function HostTelemetryStateBadge({ state }: Props) {
  const normalizedState: HostTelemetryFreshnessState = state ?? 'unknown';
  return <Badge tone={TONES[normalizedState]}>{LABELS[normalizedState]}</Badge>;
}
