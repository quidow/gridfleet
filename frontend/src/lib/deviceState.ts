import type { DeviceChipStatus, DeviceHold, DeviceOperationalState } from '../types';

export const DEVICE_STATUSES: DeviceChipStatus[] = [
  'available',
  'busy',
  'offline',
  'maintenance',
  'reserved',
  'verifying',
];
export const OPERATIONAL_STATES: DeviceOperationalState[] = ['available', 'busy', 'offline', 'verifying'];
export const DEVICE_HOLDS: DeviceHold[] = ['maintenance', 'reserved'];

export function deviceChipStatus(device: {
  operational_state: DeviceOperationalState;
  hold: DeviceHold | null;
}): DeviceChipStatus {
  if (device.operational_state === 'busy' || device.operational_state === 'verifying') {
    return device.operational_state;
  }
  return device.hold ?? device.operational_state;
}
