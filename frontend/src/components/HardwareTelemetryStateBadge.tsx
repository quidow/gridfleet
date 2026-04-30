import type { HardwareTelemetryState } from '../types';
import {
  HARDWARE_TELEMETRY_STATE_LABELS,
  HARDWARE_TELEMETRY_STATE_STYLES,
} from '../lib/hardwareTelemetry';

type Props = {
  state: HardwareTelemetryState | undefined;
};

export default function HardwareTelemetryStateBadge({ state }: Props) {
  const normalizedState = state ?? 'unknown';
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${HARDWARE_TELEMETRY_STATE_STYLES[normalizedState]}`}>
      {HARDWARE_TELEMETRY_STATE_LABELS[normalizedState]}
    </span>
  );
}
