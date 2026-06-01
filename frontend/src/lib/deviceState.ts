import type { DeviceOperationalState } from '../types';

export const DEVICE_STATUSES: DeviceOperationalState[] = [
  'available',
  'busy',
  'offline',
  'maintenance',
  'verifying',
];

export function deviceChipStatus(device: {
  operational_state: DeviceOperationalState;
}): DeviceOperationalState {
  return device.operational_state;
}
