import type { DeviceChipStatus, DeviceHold, DeviceOperationalState } from '../types';

export const DEVICE_STATUSES: DeviceChipStatus[] = ['available', 'busy', 'offline', 'maintenance', 'reserved'];
export const OPERATIONAL_STATES: DeviceOperationalState[] = ['available', 'busy', 'offline'];
export const DEVICE_HOLDS: DeviceHold[] = ['maintenance', 'reserved'];

export function deviceChipStatus(device: {
  operational_state: DeviceOperationalState;
  hold: DeviceHold | null;
}): DeviceChipStatus {
  if (device.operational_state === 'busy') {
    return 'busy';
  }
  return device.hold ?? device.operational_state;
}
