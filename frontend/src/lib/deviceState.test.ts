import { describe, expect, it } from 'vitest';
import { deviceChipStatus } from './deviceState';
import type { DeviceHold, DeviceOperationalState } from '../types';

describe('deviceChipStatus', () => {
  it('returns hold when operational_state is not busy', () => {
    expect(deviceChipStatus({
      operational_state: 'available' as DeviceOperationalState,
      hold: 'reserved' as DeviceHold,
    })).toBe('reserved');
    expect(deviceChipStatus({
      operational_state: 'offline' as DeviceOperationalState,
      hold: 'reserved' as DeviceHold,
    })).toBe('reserved');
    expect(deviceChipStatus({
      operational_state: 'available' as DeviceOperationalState,
      hold: 'maintenance' as DeviceHold,
    })).toBe('maintenance');
  });

  it('returns busy when operational_state is busy, regardless of hold', () => {
    expect(deviceChipStatus({
      operational_state: 'busy' as DeviceOperationalState,
      hold: 'reserved' as DeviceHold,
    })).toBe('busy');
    expect(deviceChipStatus({
      operational_state: 'busy' as DeviceOperationalState,
      hold: 'maintenance' as DeviceHold,
    })).toBe('busy');
  });

  it('returns operational_state when hold is null', () => {
    expect(deviceChipStatus({
      operational_state: 'available' as DeviceOperationalState,
      hold: null,
    })).toBe('available');
    expect(deviceChipStatus({
      operational_state: 'busy' as DeviceOperationalState,
      hold: null,
    })).toBe('busy');
    expect(deviceChipStatus({
      operational_state: 'offline' as DeviceOperationalState,
      hold: null,
    })).toBe('offline');
  });
});
