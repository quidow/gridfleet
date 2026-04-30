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
      return 'Suppressed';
    case 'backoff':
      return 'Backing Off';
    case 'waiting_for_session_end':
      return 'Waiting For Session End';
    case 'manual':
      return 'Manual Recovery';
    default:
      return 'Idle';
  }
}

export function duration(start: string, end: string | null): string {
  const endMs = end ? new Date(end).getTime() : Date.now();
  const diff = endMs - new Date(start).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

export function managedDeviceConfigKeys(fields: Array<{ id: string }>): Set<string> {
  return new Set(fields.map((field) => field.id));
}

export function omitManagedDeviceConfig(
  config: Record<string, unknown> | undefined,
  managedKeys: Set<string>,
): Record<string, unknown> | undefined {
  if (config === undefined) return undefined;
  return Object.fromEntries(Object.entries(config).filter(([key]) => !managedKeys.has(key)));
}

export function restoreManagedDeviceConfig(
  editableConfig: Record<string, unknown>,
  sourceConfig: Record<string, unknown> | undefined,
  managedKeys: Set<string>,
): Record<string, unknown> {
  const restored = { ...editableConfig };
  for (const key of managedKeys) {
    if (sourceConfig && key in sourceConfig) {
      restored[key] = sourceConfig[key];
    }
  }
  return restored;
}
