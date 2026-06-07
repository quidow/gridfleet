import type { DeviceDetail, DeviceRead, HealthVerdictRead, HealthVerdictStatus } from '../../types';
import type { SummaryPillTone } from '../../components/ui';
import {
  HARDWARE_HEALTH_STATUS_LABELS,
  HARDWARE_TELEMETRY_STATE_LABELS,
} from '../../lib/hardwareTelemetry';
import { VERDICT_STATUS_LABELS } from '../../lib/healthVerdicts';
import { formatDateTime } from '../../utils/dateFormatting';

const VERDICT_PILL_TONES: Record<HealthVerdictStatus, SummaryPillTone> = {
  ok: 'ok',
  warn: 'warn',
  failed: 'error',
  unknown: 'neutral',
};

export type DeviceDetailStatusPill = {
  key: 'hardware' | 'device' | 'node' | 'viability';
  label: string;
  value: string;
  tone: SummaryPillTone;
  to?: string;
  title?: string;
};

function hardwareDetail(
  telemetryState: DeviceRead['hardware_telemetry_state'],
  reportedAt: string | null,
): string {
  switch (telemetryState) {
    case 'fresh':
      return reportedAt ? `Reported ${formatDateTime(reportedAt)}` : 'Fresh hardware telemetry.';
    case 'stale':
      return reportedAt ? `Last report ${formatDateTime(reportedAt)}` : 'Latest hardware snapshot is stale.';
    case 'unsupported':
      return 'Host agent does not report battery telemetry for this device.';
    default:
      return 'No hardware telemetry reported yet.';
  }
}

export function hardwareSummary(
  device: Pick<
    DeviceRead,
    'hardware_health_status' | 'hardware_telemetry_state' | 'hardware_telemetry_reported_at'
  >,
): {
  value: string;
  tone: SummaryPillTone;
  detail: string;
  to?: string;
} {
  if (device.hardware_telemetry_state === 'unsupported') {
    return {
      value: HARDWARE_TELEMETRY_STATE_LABELS.unsupported,
      tone: 'neutral',
      detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
    };
  }

  if (device.hardware_health_status === 'critical') {
    return {
      value: HARDWARE_HEALTH_STATUS_LABELS.critical,
      tone: 'error',
      detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
      to: '/devices?hardware_health_status=critical',
    };
  }

  if (device.hardware_health_status === 'warning') {
    return {
      value: HARDWARE_HEALTH_STATUS_LABELS.warning,
      tone: 'warn',
      detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
      to: '/devices?hardware_health_status=warning',
    };
  }

  if (device.hardware_telemetry_state === 'stale') {
    return {
      value: HARDWARE_TELEMETRY_STATE_LABELS.stale,
      tone: 'warn',
      detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
      to: '/devices?hardware_telemetry_state=stale',
    };
  }

  if (
    device.hardware_health_status === 'healthy'
    && device.hardware_telemetry_state === 'fresh'
  ) {
    return {
      value: HARDWARE_HEALTH_STATUS_LABELS.healthy,
      tone: 'ok',
      detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
    };
  }

  return {
    value:
      device.hardware_telemetry_state === 'unknown'
        ? HARDWARE_TELEMETRY_STATE_LABELS.unknown
        : HARDWARE_HEALTH_STATUS_LABELS[device.hardware_health_status],
    tone: 'neutral',
    detail: hardwareDetail(device.hardware_telemetry_state, device.hardware_telemetry_reported_at),
  };
}

function verdictPill(
  key: 'device' | 'node' | 'viability',
  label: string,
  verdict: HealthVerdictRead,
  to: string,
): DeviceDetailStatusPill {
  return {
    key,
    label,
    tone: VERDICT_PILL_TONES[verdict.status],
    value: verdict.detail || VERDICT_STATUS_LABELS[verdict.status],
    title: verdict.checked_at ? `Last checked ${formatDateTime(verdict.checked_at)}` : undefined,
    to,
  };
}

export function getDeviceDetailStatusPills(
  device: Pick<
    DeviceDetail,
    'id'
    | 'hardware_health_status'
    | 'hardware_telemetry_state'
    | 'hardware_telemetry_reported_at'
    | 'health_summary'
  >,
): DeviceDetailStatusPill[] {
  const hardware = hardwareSummary(device);
  const hs = device.health_summary;
  const triageTo = `/devices/${device.id}?tab=triage#device-health`;

  return [
    {
      key: 'hardware',
      label: 'Hardware',
      tone: hardware.tone,
      value: hardware.value,
      title: hardware.detail,
      to: hardware.to,
    },
    verdictPill('device', 'Device', hs.device, triageTo),
    verdictPill('node', 'Node', hs.node, triageTo),
    verdictPill('viability', 'Viability', hs.viability, triageTo),
  ];
}
