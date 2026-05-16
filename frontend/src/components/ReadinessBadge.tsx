import type { DeviceReadinessState } from '../types';
import Badge, { type BadgeTone } from './ui/Badge';
import { readinessLabel } from './readiness';

const READINESS_TONE_MAP: Record<DeviceReadinessState, BadgeTone> = {
  setup_required: 'warning',
  verification_required: 'info',
  verified: 'success',
};

export function ReadinessBadge({ state }: { state: DeviceReadinessState }) {
  const tone = READINESS_TONE_MAP[state] ?? 'neutral';
  return (
    <Badge tone={tone}>
      {readinessLabel(state)}
    </Badge>
  );
}
