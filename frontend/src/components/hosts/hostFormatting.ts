import { formatDateTime, formatRelativeTime } from '../../utils/dateFormatting';

export function formatHostLastHeartbeat(dateStr: string | null): string {
  if (!dateStr) return 'Never';
  return formatRelativeTime(dateStr);
}

export function formatHostTimestamp(dateStr: string | null): string {
  return formatDateTime(dateStr);
}
