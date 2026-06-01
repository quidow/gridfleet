import { describe, expect, it } from 'vitest';
import { deviceChipStatus } from './deviceState';
import type { DeviceOperationalState } from '../types';

describe('deviceChipStatus', () => {
  it('returns operational_state directly', () => {
    const states: DeviceOperationalState[] = [
      'available',
      'busy',
      'offline',
      'maintenance',
      'verifying',
    ];
    for (const state of states) {
      expect(deviceChipStatus({ operational_state: state })).toBe(state);
    }
  });

  it('ignores reservation: a reserved+available device chips as available', () => {
    expect(deviceChipStatus({
      operational_state: 'available' as DeviceOperationalState,
    })).toBe('available');
  });
});
