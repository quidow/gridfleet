import Badge from '../ui/Badge';
import { EVENT_SEVERITY_LABEL, severityForEventType } from './eventRegistry';

interface SeverityBadgeProps {
  eventType: string;
}

export default function SeverityBadge({ eventType }: SeverityBadgeProps) {
  const severity = severityForEventType(eventType);
  return (
    <Badge tone={severity} dot>
      {EVENT_SEVERITY_LABEL[severity]}
    </Badge>
  );
}
