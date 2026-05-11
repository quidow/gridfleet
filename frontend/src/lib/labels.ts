import type { ConnectionType, DeviceChipStatus, DeviceType, SessionStatus } from '../types';

/**
 * Resolve a human-readable platform label from the catalog label (preferred),
 * or return a readable platform_id fallback.
 */
export function resolvePlatformLabel(
  platformId: string | null | undefined,
  platformLabel: string | null | undefined,
): string {
  if (!platformLabel) return platformId ? humanizeIdentifier(platformId) : '-';
  const withoutQualifier = platformLabel
    .replace(/\s+\((?:network|usb|virtual|real|real device|emulator|simulator)\)\s*$/i, '')
    .trim();
  return withoutQualifier || (platformId ? humanizeIdentifier(platformId) : '-');
}

function humanizeIdentifier(value: string): string {
  const withoutQualifier = value
    .replace(/[_-\s]+real[_-\s]+device$/i, '')
    .replace(/(?:[_-\s]+(?:network|usb|virtual|real|emulator|simulator))+$/i, '');

  return withoutQualifier
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map(formatIdentifierPart)
    .join(' ');
}

function formatIdentifierPart(part: string): string {
  const lower = part.toLowerCase();
  if (lower === 'id') return 'ID';
  if (/^tvos$/i.test(part)) return 'TVOS';
  if (lower.length <= 3) return lower.toUpperCase();
  if (lower.endsWith('tv')) {
    const prefix = lower.slice(0, -2);
    return `${prefix.charAt(0).toUpperCase()}${prefix.slice(1)} TV`;
  }
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

export const DEVICE_STATUS_LABELS: Record<DeviceChipStatus, string> = {
  available: 'Available',
  busy: 'Busy',
  offline: 'Offline',
  maintenance: 'Maintenance',
  reserved: 'Reserved',
  verifying: 'Verifying',
};

export const SESSION_STATUS_LABELS: Record<SessionStatus, string> = {
  running: 'Running',
  passed: 'Passed',
  failed: 'Failed',
  error: 'Error',
};

export const DEVICE_TYPE_LABELS: Record<DeviceType, string> = {
  real_device: 'Real Device',
  emulator: 'Emulator',
  simulator: 'Simulator',
};

export const CONNECTION_TYPE_LABELS: Record<ConnectionType, string> = {
  usb: 'USB',
  network: 'Network',
  virtual: 'Virtual',
};
