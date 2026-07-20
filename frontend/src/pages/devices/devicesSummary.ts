import type { SummaryPillTone } from '../../components/ui';
import type {
  DeviceChipStatus,
  DeviceFilterStatus,
  DeviceRead,
} from '../../types';
import { deviceChipStatus } from '../../lib/deviceState';

const SUMMARY_PARAM_KEYS = [
  'status',
  'reserved',
  'needs_attention',
] as const;

export interface DevicesSummaryStats {
  total: number;
  available: number;
  busy: number;
  reserved: number;
  offline: number;
  maintenance: number;
  attentionCount: number;
}

interface DevicesSummaryHrefOptions {
  status?: DeviceFilterStatus | null;
  reserved?: boolean;
  needsAttention?: boolean;
}

function countByAvailabilityStatus(devices: DeviceRead[], status: DeviceChipStatus) {
  return devices.filter((device) => {
    const chipStatus = deviceChipStatus(device);
    return status === 'busy' ? chipStatus === 'busy' || chipStatus === 'verifying' : chipStatus === status;
  }).length;
}

export function deriveDevicesSummaryStats(devices: DeviceRead[]): DevicesSummaryStats {
  return {
    total: devices.length,
    available: countByAvailabilityStatus(devices, 'available'),
    busy: countByAvailabilityStatus(devices, 'busy'),
    reserved: devices.filter((device) => device.is_reserved).length,
    offline: countByAvailabilityStatus(devices, 'offline'),
    maintenance: countByAvailabilityStatus(devices, 'maintenance'),
    attentionCount: devices.filter((device) => device.needs_attention).length,
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
  if (options.reserved) {
    nextParams.set('reserved', 'true');
  }
  if (options.needsAttention) {
    nextParams.set('needs_attention', 'true');
  }

  const query = nextParams.toString();
  return query ? `/devices?${query}` : '/devices';
}
