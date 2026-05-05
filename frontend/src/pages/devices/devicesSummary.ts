import type { SummaryPillTone } from '../../components/ui';
import type {
  DeviceChipStatus,
  DeviceRead,
  HardwareHealthStatus,
  HardwareTelemetryState,
} from '../../types';
import { deviceChipStatus } from '../../lib/deviceState';

const SUMMARY_PARAM_KEYS = [
  'status',
  'needs_attention',
  'hardware_health_status',
  'hardware_telemetry_state',
] as const;

export interface DevicesSummaryStats {
  total: number;
  available: number;
  busy: number;
  reserved: number;
  offline: number;
  maintenance: number;
  attentionCount: number;
  hardwareCritical: number;
  hardwareWarning: number;
  telemetryStale: number;
}

interface DevicesSummaryHrefOptions {
  status?: DeviceChipStatus | null;
  needsAttention?: boolean;
  hardwareHealthStatus?: HardwareHealthStatus | null;
  hardwareTelemetryState?: HardwareTelemetryState | null;
}

function countByAvailabilityStatus(devices: DeviceRead[], status: DeviceChipStatus) {
  return devices.filter((device) => deviceChipStatus(device) === status).length;
}

export function deriveDevicesSummaryStats(devices: DeviceRead[]): DevicesSummaryStats {
  return {
    total: devices.length,
    available: countByAvailabilityStatus(devices, 'available'),
    busy: countByAvailabilityStatus(devices, 'busy'),
    reserved: countByAvailabilityStatus(devices, 'reserved'),
    offline: countByAvailabilityStatus(devices, 'offline'),
    maintenance: countByAvailabilityStatus(devices, 'maintenance'),
    attentionCount: devices.filter((device) => device.needs_attention).length,
    hardwareCritical: devices.filter((device) => device.hardware_health_status === 'critical').length,
    hardwareWarning: devices.filter((device) => device.hardware_health_status === 'warning').length,
    telemetryStale: devices.filter((device) => device.hardware_telemetry_state === 'stale').length,
  };
}

export function getAttentionTone(stats: DevicesSummaryStats): SummaryPillTone {
  return stats.attentionCount > 0 ? 'warn' : 'neutral';
}

export function getAttentionHrefOptions(): DevicesSummaryHrefOptions {
  return { needsAttention: true };
}

export function buildDevicesSummaryHref(
  searchParams: URLSearchParams,
  options: DevicesSummaryHrefOptions = {},
): string {
  const nextParams = new URLSearchParams(searchParams);

  for (const key of SUMMARY_PARAM_KEYS) {
    nextParams.delete(key);
  }

  if (options.status) {
    nextParams.set('status', options.status);
  }
  if (options.needsAttention) {
    nextParams.set('needs_attention', 'true');
  }
  if (options.hardwareHealthStatus) {
    nextParams.set('hardware_health_status', options.hardwareHealthStatus);
  }
  if (options.hardwareTelemetryState) {
    nextParams.set('hardware_telemetry_state', options.hardwareTelemetryState);
  }

  const query = nextParams.toString();
  return query ? `/devices?${query}` : '/devices';
}
