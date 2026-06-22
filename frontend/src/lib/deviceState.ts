import type { BadgeTone } from '../components/ui/Badge';
import type { DeviceChipStatus, DeviceFilterStatus, DeviceOperationalState } from '../types';

export const DEVICE_STATUSES: DeviceOperationalState[] = [
  'available',
  'busy',
  'offline',
  'maintenance',
  'verifying',
];

// Device-list status filter values: the operational states. Reservation is an
// orthogonal boolean filter (`reserved=true`), not a status value.
export const DEVICE_FILTER_STATUSES: DeviceFilterStatus[] = [
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

// Single source of truth for operational-state → badge tone. Shared by the
// Devices table (AvailabilityCell), the dashboard ActivityCard, and the Router
// node cards so the same state never renders two colours across pages.
// `verifying` is info (blue) — kept distinct from busy (warning/amber) — so an
// operator can tell "occupied" from "checking" by colour alone.
export const OPERATIONAL_STATE_TONE: Record<DeviceOperationalState, BadgeTone> = {
  available: 'success',
  busy: 'warning',
  verifying: 'info',
  offline: 'critical',
  maintenance: 'neutral',
};

export function availabilityTone(status: DeviceChipStatus): BadgeTone {
  return OPERATIONAL_STATE_TONE[status];
}
