import type { DeviceRead } from '../types';

export type UnifiedHealthTone = 'ok' | 'warn' | 'error' | 'unknown';

interface UnifiedHealth {
  tone: UnifiedHealthTone;
  label: string;
  reasons: string[];
  summary: string;
}

const TONE_LABELS: Record<UnifiedHealthTone, string> = {
  ok: 'Healthy',
  warn: 'Warning',
  error: 'Unhealthy',
  unknown: 'Unknown',
};

export function deriveUnifiedHealth(device: DeviceRead): UnifiedHealth {
  const liveness = device.health_summary?.healthy ?? null;
  const livenessDetail = device.health_summary?.summary?.trim() ?? '';
  const hardware = device.hardware_health_status;
  const telemetry = device.hardware_telemetry_state;
  const lifecycle = device.lifecycle_policy_summary?.state;
  const readiness = device.readiness_state;

  const reasons: string[] = [];
  let tone: UnifiedHealthTone = 'ok';

  if (device.review_required) {
    reasons.push(device.review_reason || 'Operator review required');
    tone = 'error';
  }

  if (liveness === false) {
    reasons.push(livenessDetail || 'Device unhealthy');
    tone = 'error';
  }

  if (lifecycle === 'suppressed') {
    reasons.push(device.lifecycle_policy_summary?.detail || 'Recovery paused — admin review needed');
    tone = 'error';
  } else if (lifecycle === 'manual') {
    reasons.push(device.lifecycle_policy_summary?.detail || 'Manual recovery requested');
    tone = 'error';
  } else if (lifecycle === 'backoff') {
    reasons.push(device.lifecycle_policy_summary?.detail || 'Recovery backoff active');
    if (tone === 'ok') tone = 'warn';
  } else if (lifecycle === 'recoverable') {
    reasons.push(device.lifecycle_policy_summary?.detail || 'Recovery eligible');
    if (tone === 'ok') tone = 'warn';
  }

  if (hardware === 'critical') {
    reasons.push('Hardware critical');
    if (tone !== 'error') tone = 'error';
  } else if (hardware === 'warning') {
    reasons.push('Hardware warning');
    if (tone === 'ok') tone = 'warn';
  }

  if (telemetry === 'stale') {
    reasons.push('Telemetry stale');
    if (tone === 'ok') tone = 'warn';
  }

  if (readiness === 'setup_required') {
    reasons.push('Setup required');
    if (tone === 'ok') tone = 'warn';
  } else if (readiness === 'verification_required') {
    reasons.push('Pending verification');
    if (tone === 'ok') tone = 'warn';
  }

  if (reasons.length > 0) {
    return buildResult(tone, reasons);
  }

  if (liveness === true) {
    return buildResult('ok', ['Healthy']);
  }

  if (liveness === null && hardware === 'unknown') {
    return buildResult('unknown', ['Health unknown']);
  }

  return buildResult('unknown', ['Health unknown']);
}

function buildResult(tone: UnifiedHealthTone, reasons: string[]): UnifiedHealth {
  return {
    tone,
    label: TONE_LABELS[tone],
    reasons,
    summary: reasons.join(' · '),
  };
}
