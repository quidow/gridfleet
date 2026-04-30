import { isEmulatorRunning, isEmulatorStopped } from '../../lib/emulatorState';
import {
  HARDWARE_HEALTH_STATUS_LABELS,
  HARDWARE_TELEMETRY_STATE_LABELS,
} from '../../lib/hardwareTelemetry';
import { DEVICE_AVAILABILITY_LABELS } from '../../lib/labels';
import type { DeviceDetail, DeviceHealth } from '../../types';
import { formatDateTime } from '../../utils/dateFormatting';

export type DeviceDetailTriageTone = 'ok' | 'warn' | 'error' | 'neutral';

export type DeviceDetailTriageActionKind =
  | 'verify'
  | 'open-run'
  | 'launch-emulator'
  | 'boot-simulator'
  | 'start-node'
  | 'open-control'
  | 'open-hardware-filter'
  | 'test-session'
  | 'none';

export interface DeviceDetailTriageAction {
  kind: DeviceDetailTriageActionKind;
  label: string;
  to?: string;
}

export interface DeviceDetailTriageEvidence {
  label: string;
  value: string;
  tone?: DeviceDetailTriageTone;
}

export interface DeviceDetailTriage {
  tone: DeviceDetailTriageTone;
  eyebrow: string;
  title: string;
  detail: string;
  action: DeviceDetailTriageAction;
  evidence: DeviceDetailTriageEvidence[];
}

type Options = {
  health?: DeviceHealth;
  canTestSession: boolean;
};

function readable(value: string | null | undefined, fallback = '-'): string {
  return value && value.trim() ? value : fallback;
}

function virtualDeviceStopped(device: DeviceDetail): boolean {
  if (device.device_type !== 'emulator' && device.device_type !== 'simulator') {
    return false;
  }
  if (isEmulatorRunning(device.emulator_state)) {
    return false;
  }
  return isEmulatorStopped(device.emulator_state) || device.availability_status === 'offline';
}

function hardwareTone(device: DeviceDetail): DeviceDetailTriageTone | null {
  if (device.hardware_health_status === 'critical') return 'error';
  if (device.hardware_health_status === 'warning') return 'warn';
  if (device.hardware_telemetry_state === 'stale') return 'warn';
  return null;
}

function hardwareFilterTarget(device: DeviceDetail): string {
  if (device.hardware_health_status === 'critical' || device.hardware_health_status === 'warning') {
    return `/devices?hardware_health_status=${device.hardware_health_status}`;
  }
  return `/devices?hardware_telemetry_state=${device.hardware_telemetry_state}`;
}

function failedHealthDetail(device: DeviceDetail, health?: DeviceHealth): string {
  const checkDetail = health?.device_checks.detail;
  if (typeof checkDetail === 'string' && checkDetail) {
    return checkDetail;
  }
  if (device.health_summary.summary) {
    return device.health_summary.summary;
  }
  return 'Device health checks are failing.';
}

export function deriveDeviceDetailTriage(
  device: DeviceDetail,
  { health, canTestSession }: Options,
): DeviceDetailTriage {
  const reservation = device.reservation;
  const node = device.appium_node;

  if (reservation?.excluded) {
    return {
      tone: 'warn',
      eyebrow: 'Run exclusion',
      title: 'Excluded from reserved run',
      detail: reservation.exclusion_reason || 'This device is held out of the active run.',
      action: { kind: 'open-run', label: 'Open Run', to: `/runs/${reservation.run_id}` },
      evidence: [
        { label: 'Run', value: reservation.run_name, tone: 'warn' },
        { label: 'Reason', value: reservation.exclusion_reason || 'Excluded', tone: 'warn' },
      ],
    };
  }

  if (virtualDeviceStopped(device)) {
    const isSimulator = device.device_type === 'simulator';
    const noun = isSimulator ? 'Simulator' : 'Emulator';
    return {
      tone: 'error',
      eyebrow: 'Virtual device stopped',
      title: `${noun} is not running`,
      detail: `${noun} must be running before the Appium node can serve sessions.`,
      action: {
        kind: isSimulator ? 'boot-simulator' : 'launch-emulator',
        label: isSimulator ? 'Boot Simulator' : 'Launch Emulator',
      },
      evidence: [
        { label: 'Target', value: readable(device.connection_target), tone: 'neutral' },
        { label: 'State', value: readable(device.emulator_state, 'offline'), tone: 'error' },
      ],
    };
  }

  if (!node || node.state !== 'running') {
    const inMaintenance = device.availability_status === 'maintenance';
    const nodeAction = reservation || inMaintenance
      ? { kind: 'open-control' as const, label: 'Review Control', to: `/devices/${device.id}?tab=control` }
      : { kind: 'start-node' as const, label: 'Start Node' };

    let tone: DeviceDetailTriageTone;
    let eyebrow: string;
    if (inMaintenance) {
      tone = 'neutral';
      eyebrow = 'Maintenance';
    } else if (!node) {
      tone = 'neutral';
      eyebrow = 'Node idle';
    } else {
      tone = 'warn';
      eyebrow = 'Device control';
    }

    return {
      tone,
      eyebrow,
      title: node ? 'Appium node is stopped' : 'No Appium node configured',
      detail: 'Start the node to register this device with Selenium Grid.',
      action: nodeAction,
      evidence: [
        { label: 'Availability', value: DEVICE_AVAILABILITY_LABELS[device.availability_status], tone: 'neutral' },
        { label: 'Node state', value: node?.state ?? 'none', tone: node ? 'warn' : 'neutral' },
      ],
    };
  }

  if (health?.healthy === false || device.health_summary.healthy === false) {
    return {
      tone: 'error',
      eyebrow: 'Health check',
      title: 'Device health check failed',
      detail: failedHealthDetail(device, health),
      action: { kind: 'open-control', label: 'Review Control', to: `/devices/${device.id}?tab=control` },
      evidence: [
        { label: 'Connectivity', value: device.health_summary.summary || 'Unhealthy', tone: 'error' },
        {
          label: 'Last checked',
          value: device.health_summary.last_checked_at
            ? formatDateTime(device.health_summary.last_checked_at)
            : '-',
          tone: 'neutral',
        },
      ],
    };
  }

  const hardwareIssueTone = hardwareTone(device);
  if (hardwareIssueTone) {
    const value = device.hardware_telemetry_state === 'stale'
        ? HARDWARE_TELEMETRY_STATE_LABELS.stale
        : HARDWARE_HEALTH_STATUS_LABELS[device.hardware_health_status];
    return {
      tone: hardwareIssueTone,
      eyebrow: 'Hardware telemetry',
      title: `Hardware ${value}`,
      detail: 'Review the latest hardware snapshot before routing long sessions.',
      action: {
        kind: 'open-hardware-filter',
        label: 'View Affected Devices',
        to: hardwareFilterTarget(device),
      },
      evidence: [
        { label: 'Health', value: HARDWARE_HEALTH_STATUS_LABELS[device.hardware_health_status], tone: hardwareIssueTone },
        { label: 'Telemetry', value: HARDWARE_TELEMETRY_STATE_LABELS[device.hardware_telemetry_state], tone: hardwareIssueTone },
      ],
    };
  }

  const telemetryNotReported = device.hardware_telemetry_state === 'unsupported';

  return {
    tone: 'ok',
    eyebrow: 'Ready',
    title: 'Device ready for sessions',
    detail: telemetryNotReported
      ? 'Readiness, availability, lifecycle, and connectivity are clear. Battery telemetry is not reported by this host.'
      : 'Readiness, availability, lifecycle, and telemetry are clear.',
    action: canTestSession
      ? { kind: 'test-session', label: 'Test Session' }
      : { kind: 'open-control', label: 'View Control', to: `/devices/${device.id}?tab=control` },
    evidence: [
      { label: 'Availability', value: DEVICE_AVAILABILITY_LABELS[device.availability_status], tone: 'ok' },
      ...(telemetryNotReported
        ? [{ label: 'Telemetry', value: HARDWARE_TELEMETRY_STATE_LABELS.unsupported, tone: 'neutral' as const }]
        : []),
    ],
  };
}
