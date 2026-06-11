import { describe, expect, it } from 'vitest';
import { availabilityTone, deviceChipStatus } from './deviceState';
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

describe('availabilityTone', () => {
  it('maps every chip status to a badge tone', () => {
    expect(availabilityTone('available')).toBe('success');
    expect(availabilityTone('busy')).toBe('warning');
    expect(availabilityTone('verifying')).toBe('warning');
    expect(availabilityTone('offline')).toBe('critical');
    expect(availabilityTone('maintenance')).toBe('neutral');
  });
});
