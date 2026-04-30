import type {
  HardwareChargingState,
  HardwareHealthStatus,
  HardwareTelemetryState,
} from '../types';

export const HARDWARE_HEALTH_STATUS_LABELS: Record<HardwareHealthStatus, string> = {
  unknown: 'Unknown',
  healthy: 'Healthy',
  warning: 'Warning',
  critical: 'Critical',
};

export const HARDWARE_HEALTH_STATUS_STYLES: Record<HardwareHealthStatus, string> = {
  unknown: 'bg-neutral-soft text-neutral-foreground',
  healthy: 'bg-success-soft text-success-foreground',
  warning: 'bg-warning-soft text-warning-foreground',
  critical: 'bg-danger-soft text-danger-foreground',
};

export const HARDWARE_TELEMETRY_STATE_LABELS: Record<HardwareTelemetryState, string> = {
  unknown: 'Unknown',
  fresh: 'Fresh',
  stale: 'Stale',
  unsupported: 'No telemetry',
};

export const HARDWARE_TELEMETRY_STATE_STYLES: Record<HardwareTelemetryState, string> = {
  unknown: 'bg-neutral-soft text-neutral-foreground',
  fresh: 'bg-info-soft text-info-foreground',
  stale: 'bg-warning-soft text-warning-foreground',
  unsupported: 'bg-neutral-soft text-neutral-foreground',
};

const CHARGING_STATE_LABELS: Record<HardwareChargingState, string> = {
  charging: 'Charging',
  discharging: 'Discharging',
  full: 'Full',
  not_charging: 'Not charging',
  unknown: 'Unknown',
};

export function formatBatteryLevel(level: number | null | undefined): string {
  return level === null || level === undefined ? '—' : `${level}%`;
}

export function formatBatteryTemperature(temperature: number | null | undefined): string {
  return temperature === null || temperature === undefined ? '—' : `${temperature.toFixed(1)}C`;
}

export function formatChargingState(state: HardwareChargingState | null | undefined): string {
  return state === null || state === undefined ? '—' : CHARGING_STATE_LABELS[state];
}
