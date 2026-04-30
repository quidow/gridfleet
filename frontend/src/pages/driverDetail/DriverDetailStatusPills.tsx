import Badge, { type BadgeTone } from '../../components/ui/Badge';
import type { DriverPack } from '../../types/driverPacks';

const STATE_TONES: Record<string, BadgeTone> = {
  enabled: 'success',
  draining: 'warning',
  disabled: 'neutral',
  draft: 'neutral',
};

export default function DriverDetailStatusPills({ pack }: { pack: DriverPack }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge tone={STATE_TONES[pack.state] ?? 'neutral'}>{pack.state}</Badge>
      {pack.current_release && <Badge tone="neutral">v{pack.current_release}</Badge>}
      {pack.platforms && (
        <Badge tone="neutral">
          {pack.platforms.length} platform{pack.platforms.length !== 1 ? 's' : ''}
        </Badge>
      )}
      {pack.runtime_policy && <Badge tone="neutral">{pack.runtime_policy.strategy}</Badge>}
    </div>
  );
}
