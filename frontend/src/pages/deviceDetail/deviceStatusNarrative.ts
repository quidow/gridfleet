import type { DeviceRead } from '../../types';

export type StatusActionKind = 'retry' | 'maintenance' | 'setup' | 'verify' | 'exit-maintenance';

export interface StatusAction {
  kind: StatusActionKind;
  label: string;
}

export interface DeviceStatusNarrative {
  text: string;
  actions: StatusAction[];
}

function relativeFromNow(iso: string | null | undefined): string {
  if (!iso) return '';
  const diffMs = new Date(iso).getTime() - Date.now();
  const absMin = Math.max(1, Math.round(Math.abs(diffMs) / 60_000));
  if (diffMs >= 0) return `in ${absMin} min`;
  if (absMin < 60) return `${absMin} min ago`;
  return `${Math.round(absMin / 60)} hours ago`;
}

export function composeDeviceStatusNarrative(device: DeviceRead): DeviceStatusNarrative {
  const lifecycle = device.lifecycle_policy_summary.state;

  if (device.readiness_state === 'setup_required') {
    const missing = device.missing_setup_fields.length
      ? ` Missing: ${device.missing_setup_fields.join(', ')}.`
      : '';
    return {
      text: `Setup required.${missing}`,
      actions: [{ kind: 'setup', label: 'Open setup' }],
    };
  }

  if (device.readiness_state === 'verification_required') {
    return {
      text: 'Pending admin verification.',
      actions: [{ kind: 'verify', label: 'Verify' }],
    };
  }

  if (device.availability_status === 'offline') {
    if (lifecycle === 'suppressed' || lifecycle === 'manual') {
      const detail = device.lifecycle_policy_summary.detail
        ? ` (${device.lifecycle_policy_summary.detail})`
        : '';
      return {
        text:
          `Offline. Automatic recovery is paused${detail}. ` +
          `An admin needs to check this device.`,
        actions: [
          { kind: 'retry', label: 'Retry now' },
          { kind: 'maintenance', label: 'Put in maintenance' },
        ],
      };
    }
    if (lifecycle === 'backoff') {
      const next = relativeFromNow(device.lifecycle_policy_summary.backoff_until);
      const when = next ? ` Next automatic recovery attempt ${next}.` : '';
      return {
        text: `Offline.${when}`,
        actions: [{ kind: 'retry', label: 'Retry now' }],
      };
    }
    return {
      text: 'Offline.',
      actions: [{ kind: 'retry', label: 'Retry now' }],
    };
  }

  if (device.availability_status === 'busy') {
    return { text: 'Busy. Currently running a session.', actions: [] };
  }

  if (device.availability_status === 'reserved') {
    return { text: 'Reserved.', actions: [] };
  }

  if (device.availability_status === 'maintenance') {
    return {
      text: 'In maintenance.',
      actions: [{ kind: 'exit-maintenance', label: 'Take out of maintenance' }],
    };
  }

  // Default: available
  const lastChecked = device.health_summary.last_checked_at
    ? ` Last health check ${relativeFromNow(device.health_summary.last_checked_at)}.`
    : '';
  return { text: `Available.${lastChecked}`, actions: [] };
}
