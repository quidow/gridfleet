import { isEmulatorRunning, isEmulatorStopped } from '../../lib/emulatorState';
import {
  HARDWARE_HEALTH_STATUS_LABELS,
  HARDWARE_TELEMETRY_STATE_LABELS,
} from '../../lib/hardwareTelemetry';
import type { DeviceDetail, DeviceHealth } from '../../types';

export type DeviceDetailTriageTone = 'ok' | 'warn' | 'error' | 'neutral' | 'info';

export type DeviceDetailTriageActionKind =
  | 'verify'
  | 'open-run'
  | 'launch-emulator'
  | 'boot-simulator'
  | 'start-node'
  | 'open-hardware-filter'
  | 'exit-maintenance'
  | 'none';

export interface DeviceDetailTriageAction {
  kind: DeviceDetailTriageActionKind;
  label: string;
  to?: string;
}

export interface DeviceDetailTriageTitleLink {
  text: string;
  to: string;
}

export interface DeviceDetailTriage {
  tone: DeviceDetailTriageTone;
  eyebrow: string;
  title: string;
  titleLink?: DeviceDetailTriageTitleLink;
  detail: string;
  action: DeviceDetailTriageAction;
}

type Options = {
  health?: DeviceHealth;
};

function virtualDeviceStopped(device: DeviceDetail): boolean {
  if (device.device_type !== 'emulator' && device.device_type !== 'simulator') {
    return false;
  }
  if (isEmulatorRunning(device.emulator_state)) {
    return false;
  }
  return isEmulatorStopped(device.emulator_state) || device.operational_state === 'offline';
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
  const parts: string[] = [];
  const hs = device.health_summary;
  if (hs.connectivity_status === 'failed') parts.push('Connectivity failed');
  if (hs.node_status && hs.node_status !== 'running') parts.push(`Node ${hs.node_status}`);
  if (hs.session_status === 'failed') parts.push('Session probe failed');
  if (parts.length > 0) return parts.join('. ') + '.';
  return 'Device health checks are failing.';
}

export function deriveDeviceDetailTriage(
  device: DeviceDetail,
  { health }: Options,
): DeviceDetailTriage {
  const reservation = device.reservation;
  const node = device.appium_node;

  if (device.review_required) {
    return {
      tone: 'error',
      eyebrow: 'Review required',
      title: 'Device shelved — operator review required',
      detail:
        device.review_reason ||
        'Automated recovery hit the failure threshold. Restart the node, re-verify, or exit maintenance to release the device back into the recovery loop.',
      action: { kind: 'none', label: '' },
    };
  }

  if (reservation?.excluded) {
    return {
      tone: 'warn',
      eyebrow: 'Run exclusion',
      title: 'Excluded from reserved run',
      detail: reservation.exclusion_reason || 'This device is held out of the active run.',
      action: { kind: 'open-run', label: 'Open Run', to: `/runs/${reservation.run_id}` },
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
    };
  }

  if (!node || node.effective_state !== 'running') {
    const inMaintenance = device.hold === 'maintenance';
    const connectivityFailed = device.health_summary.connectivity_status === 'failed'
      || device.health_summary.healthy === false;

    let nodeAction: DeviceDetailTriageAction;
    if (inMaintenance) {
      nodeAction = { kind: 'exit-maintenance', label: 'Take out of maintenance' };
    } else if (reservation) {
      nodeAction = { kind: 'none', label: '' };
    } else {
      nodeAction = { kind: 'start-node', label: 'Start Node' };
    }

    let tone: DeviceDetailTriageTone;
    let eyebrow: string;
    let title: string;
    let titleLink: DeviceDetailTriageTitleLink | undefined;
    let detail: string;

    if (inMaintenance) {
      const maintenanceReason = device.lifecycle_policy_summary.maintenance_reason;
      tone = 'neutral';
      eyebrow = 'Maintenance';
      title = 'In maintenance';
      detail = maintenanceReason || 'Device is in maintenance mode.';
    } else if (connectivityFailed) {
      tone = 'error';
      eyebrow = 'Connectivity';
      title = reservation ? 'Device connectivity lost — reserved by' : 'Device connectivity lost';
      titleLink = reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined;
      detail = failedHealthDetail(device);
    } else if (!node) {
      tone = 'neutral';
      eyebrow = 'Node idle';
      title = reservation ? 'No Appium node configured — reserved by' : 'No Appium node configured';
      titleLink = reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined;
      detail = 'Start the node to register this device with Selenium Grid.';
    } else {
      tone = 'warn';
      eyebrow = 'Device control';
      title = reservation ? 'Appium node is stopped — reserved by' : 'Appium node is stopped';
      titleLink = reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined;
      detail = 'Start the node to register this device with Selenium Grid.';
    }

    return { tone, eyebrow, title, titleLink, detail, action: nodeAction };
  }

  if (health?.healthy === false || device.health_summary.healthy === false) {
    return {
      tone: 'error',
      eyebrow: 'Health check',
      title: reservation ? 'Device health check failed — reserved by' : 'Device health check failed',
      titleLink: reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined,
      detail: failedHealthDetail(device, health),
      action: { kind: 'none', label: '' },
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
    };
  }

  if (reservation) {
    const busy = device.operational_state === 'busy';
    return {
      tone: busy ? 'warn' : 'info',
      eyebrow: busy ? 'Busy' : 'Reserved',
      title: busy ? 'Running a session — reserved by' : 'Reserved by',
      titleLink: { text: reservation.run_name, to: `/runs/${reservation.run_id}` },
      detail: busy
        ? 'Device is actively serving a test session for this run.'
        : 'Device is held for this run. Available and waiting for sessions.',
      action: { kind: 'none', label: '' },
    };
  }

  if (device.hold === 'maintenance') {
    const mr = device.lifecycle_policy_summary.maintenance_reason;
    const draining = device.operational_state === 'busy';
    return {
      tone: draining ? 'warn' : 'neutral',
      eyebrow: draining ? 'Draining' : 'Maintenance',
      title: draining ? 'Session active — maintenance pending' : 'In maintenance',
      detail: mr || 'Device is in maintenance mode.',
      action: { kind: 'exit-maintenance', label: 'Take out of maintenance' },
    };
  }

  if (device.operational_state === 'verifying') {
    return {
      tone: 'warn',
      eyebrow: 'Verifying',
      title: 'Verification in progress',
      detail: 'A device verification job is running.',
      action: { kind: 'none', label: '' },
    };
  }

  if (device.operational_state === 'busy') {
    return {
      tone: 'warn',
      eyebrow: 'Busy',
      title: 'Running a session',
      detail: 'Device is actively serving a test session.',
      action: { kind: 'none', label: '' },
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
    action: { kind: 'none', label: '' },
  };
}
