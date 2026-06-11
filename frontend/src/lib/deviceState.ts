import type { BadgeTone } from '../components/ui/Badge';
import type { DeviceChipStatus, DeviceFilterStatus, DeviceOperationalState } from '../types';

export const DEVICE_STATUSES: DeviceOperationalState[] = [
  'available',
  'busy',
  'offline',
  'maintenance',
  'verifying',
];

// Device-list status filter values. Superset of operational states: 'reserved'
// is a server-side filter (active reservation), not an operational state.
export const DEVICE_FILTER_STATUSES: DeviceFilterStatus[] = [
  'available',
  'busy',
  'offline',
  'maintenance',
  'reserved',
  'verifying',
];

export function deviceChipStatus(device: {
  operational_state: DeviceOperationalState;
}): DeviceOperationalState {
  return device.operational_state;
}

export function availabilityTone(status: DeviceChipStatus): BadgeTone {
  switch (status) {
    case 'available': return 'success';
    case 'busy': return 'warning';
    case 'verifying': return 'warning';
    case 'offline': return 'critical';
    case 'maintenance': return 'neutral';
  }
}
