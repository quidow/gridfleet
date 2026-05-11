import Badge, { type BadgeTone } from './ui/Badge';
import { formatStatus } from '../utils/formatStatus';

const STATUS_TONE_MAP: Record<string, BadgeTone> = {
  available: 'success',
  online: 'success',
  running: 'success',
  passed: 'success',
  completed: 'success',
  busy: 'warning',
  preparing: 'warning',
  pending: 'warning',
  ready: 'info',
  active: 'info',
  completing: 'info',
  reserved: 'info',
  starting: 'info',
  stopping: 'warning',
  restarting: 'info',
  blocked: 'warning',
  offline: 'neutral',
  stopped: 'neutral',
  cancelled: 'neutral',
  maintenance: 'neutral',
  error: 'danger',
  failed: 'danger',
  expired: 'danger',
};

type StatusBadgeProps = {
  status: string;
  label?: string;
};

export default function StatusBadge({ status, label }: StatusBadgeProps) {
  const tone: BadgeTone = STATUS_TONE_MAP[status] ?? 'neutral';
  return <Badge tone={tone}>{label ?? formatStatus(status)}</Badge>;
}
