import type { DeviceDetail, DeviceHealth } from '../../types';

export type DeviceDetailTriageTone = 'ok' | 'warn' | 'error' | 'neutral' | 'info';

export type DeviceDetailTriageActionKind =
  | 'verify'
  | 'open-run'
  | 'start-node'
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

function failedHealthDetail(device: DeviceDetail, health?: DeviceHealth): string {
  const checkDetail = health?.device_checks.detail;
  if (typeof checkDetail === 'string' && checkDetail) {
    return checkDetail;
  }
  const parts: string[] = [];
  const hs = device.health_summary;
  if (hs.device.status === 'failed') parts.push(hs.device.detail || 'Device checks failed');
  if (hs.node.status === 'failed') parts.push(`Node ${hs.node.detail || 'failed'}`);
  if (hs.viability.status === 'failed') parts.push(hs.viability.detail || 'Session probe failed');
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

  if (!node || node.effective_state !== 'running') {
    const inMaintenance = device.operational_state === 'maintenance';
    const connectivityFailed = device.health_summary.device.status === 'failed';

    let nodeAction: DeviceDetailTriageAction;
    if (inMaintenance) {
      nodeAction = { kind: 'exit-maintenance', label: 'Take out of maintenance' };
    } else if (connectivityFailed || reservation) {
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
      detail = failedHealthDetail(device, health);
    } else if (!node) {
      tone = 'neutral';
      eyebrow = 'Node idle';
      title = reservation ? 'No Appium node configured — reserved by' : 'No Appium node configured';
      titleLink = reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined;
      detail = 'Start the node to make this device available for sessions.';
    } else {
      tone = 'warn';
      eyebrow = 'Device control';
      title = reservation ? 'Appium node is stopped — reserved by' : 'Appium node is stopped';
      titleLink = reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined;
      detail = 'Start the node to make this device available for sessions.';
    }

    return { tone, eyebrow, title, titleLink, detail, action: nodeAction };
  }

  if (health?.healthy === false || device.health_summary.overall === 'failed') {
    return {
      tone: 'error',
      eyebrow: 'Health check',
      title: reservation ? 'Device health check failed — reserved by' : 'Device health check failed',
      titleLink: reservation ? { text: reservation.run_name, to: `/runs/${reservation.run_id}` } : undefined,
      detail: failedHealthDetail(device, health),
      action: { kind: 'none', label: '' },
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

  const mr = device.lifecycle_policy_summary.maintenance_reason;
  const draining = device.operational_state === 'busy' && !!mr;
  if (device.operational_state === 'maintenance' || draining) {
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

  return {
    tone: 'ok',
    eyebrow: 'Ready',
    title: 'Device ready for sessions',
    detail: 'Readiness, availability, lifecycle, and connectivity are clear.',
    action: { kind: 'none', label: '' },
  };
}
