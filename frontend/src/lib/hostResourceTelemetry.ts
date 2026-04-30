import type { HardwareTelemetryState } from '../types';

export function deriveHostResourceTelemetryState(
  latestRecordedAt: string | null,
  intervalSec: number,
  nowMs = Date.now(),
): HardwareTelemetryState {
  if (!latestRecordedAt) {
    return 'unknown';
  }

  const recordedAtMs = new Date(latestRecordedAt).getTime();
  if (Number.isNaN(recordedAtMs)) {
    return 'unknown';
  }

  return nowMs - recordedAtMs <= intervalSec * 2 * 1000 ? 'fresh' : 'stale';
}
