import type { DeviceHealth, PlatformDescriptor } from '../../types';
import { formatDateTime } from '../../utils/dateFormatting';

export function getCheckLabels(descriptor: PlatformDescriptor | null): Record<string, string> {
  return Object.fromEntries((descriptor?.healthChecks ?? []).map((check) => [check.id, check.label]));
}

export function formatDate(dateStr: string | null): string {
  return formatDateTime(dateStr);
}

export function formatViabilityStatus(status: string | null | undefined): string {
  if (status === 'passed') return 'Passed';
  if (status === 'failed') return 'Failed';
  return 'Not Run';
}

export function formatRecoveryState(status: DeviceHealth['lifecycle_policy']['recovery_state'] | undefined): string {
  switch (status) {
    case 'eligible':
      return 'Eligible';
    case 'suppressed':
      return 'Recovery Paused';
    case 'backoff':
      return 'Waiting to Retry';
    case 'waiting_for_session_end':
      return 'Stopping Soon';
    case 'manual':
      return 'Manual Recovery';
    default:
      return 'Idle';
  }
}

