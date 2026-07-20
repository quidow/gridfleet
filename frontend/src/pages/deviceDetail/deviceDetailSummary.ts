import type { DeviceDetail, HealthVerdictRead, HealthVerdictStatus } from '../../types';
import type { SummaryPillTone } from '../../components/ui';
import { VERDICT_STATUS_LABELS } from '../../lib/healthVerdicts';
import { formatDateTime } from '../../utils/dateFormatting';

const VERDICT_PILL_TONES: Record<HealthVerdictStatus, SummaryPillTone> = {
  ok: 'ok',
  warn: 'warn',
  failed: 'error',
  unknown: 'neutral',
};

export type DeviceDetailStatusPill = {
  key: 'device' | 'node' | 'viability';
  label: string;
  value: string;
  tone: SummaryPillTone;
  to?: string;
  title?: string;
};

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
  device: Pick<DeviceDetail, 'id' | 'health_summary'>,
): DeviceDetailStatusPill[] {
  const hs = device.health_summary;
  const triageTo = `/devices/${device.id}?tab=triage#device-health`;

  return [
    verdictPill('device', 'Device', hs.device, triageTo),
    verdictPill('node', 'Node', hs.node, triageTo),
    verdictPill('viability', 'Viability', hs.viability, triageTo),
  ];
}
