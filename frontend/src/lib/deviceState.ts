import type { DeviceFilterStatus, DeviceOperationalState } from '../types';

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
