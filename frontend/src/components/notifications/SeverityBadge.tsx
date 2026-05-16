import Badge from '../ui/Badge';
import { EVENT_SEVERITY_LABEL, resolveEventSeverity } from './eventRegistry';
import type { EventLike } from './eventRegistry';

interface SeverityBadgeProps {
  event: EventLike;
}

export default function SeverityBadge({ event }: SeverityBadgeProps) {
  const severity = resolveEventSeverity(event);
  return (
    <Badge tone={severity} dot>
      {EVENT_SEVERITY_LABEL[severity]}
    </Badge>
  );
}
