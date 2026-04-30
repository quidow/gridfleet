import type { DeviceRead } from '../types';
import Badge, { type BadgeTone } from './ui/Badge';

const STATE_TONE_MAP: Record<DeviceRead['lifecycle_policy_summary']['state'], BadgeTone> = {
  idle: 'neutral',
  deferred_stop: 'warning',
  backoff: 'warning',
  excluded: 'danger',
  suppressed: 'warning',
  recoverable: 'info',
  manual: 'neutral',
};

export default function LifecyclePolicyBadge({
  summary,
}: {
  summary: DeviceRead['lifecycle_policy_summary'];
}) {
  const tone = STATE_TONE_MAP[summary.state] ?? 'neutral';
  return (
    <Badge tone={tone} title={summary.detail ?? summary.label}>
      {summary.label}
    </Badge>
  );
}
