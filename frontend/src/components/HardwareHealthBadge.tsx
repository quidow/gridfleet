import type { HardwareHealthStatus } from '../types';
import {
  HARDWARE_HEALTH_STATUS_LABELS,
  HARDWARE_HEALTH_STATUS_STYLES,
} from '../lib/hardwareTelemetry';

type Props = {
  status: HardwareHealthStatus | undefined;
};

export default function HardwareHealthBadge({ status }: Props) {
  const normalizedStatus = status ?? 'unknown';
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${HARDWARE_HEALTH_STATUS_STYLES[normalizedStatus]}`}>
      {HARDWARE_HEALTH_STATUS_LABELS[normalizedStatus]}
    </span>
  );
}
