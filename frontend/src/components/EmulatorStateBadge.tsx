import { Loader2 } from 'lucide-react';
import Badge, { type BadgeTone } from './ui/Badge';

const STATE_CONFIG: Record<string, { label: string; tone: BadgeTone; spinning?: boolean }> = {
  stopped: { label: 'Stopped', tone: 'neutral' },
  shutdown: { label: 'Shutdown', tone: 'neutral' },
  booting: { label: 'Booting', tone: 'warning', spinning: true },
  running: { label: 'Running', tone: 'success' },
  booted: { label: 'Booted', tone: 'success' },
};

interface Props {
  state: string | null | undefined;
  className?: string;
}

export function EmulatorStateBadge({ state, className }: Props) {
  if (!state) return null;

  const config = STATE_CONFIG[state];
  if (!config) return null;

  return (
    <Badge
      data-testid="emulator-state-badge"
      tone={config.tone}
      icon={config.spinning ? <Loader2 size={10} className="animate-spin" /> : undefined}
      className={className}
    >
      {config.label}
    </Badge>
  );
}
