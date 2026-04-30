import { describe, expect, it } from 'vitest';

import {
  formatBatteryLevel,
  formatBatteryTemperature,
  formatChargingState,
} from './hardwareTelemetry';

describe('hardware telemetry formatting', () => {
  it('formats nullable values safely', () => {
    expect(formatBatteryLevel(84)).toBe('84%');
    expect(formatBatteryLevel(null)).toBe('—');
    expect(formatBatteryTemperature(36.7)).toBe('36.7C');
    expect(formatBatteryTemperature(undefined)).toBe('—');
    expect(formatChargingState('charging')).toBe('Charging');
    expect(formatChargingState(null)).toBe('—');
  });
});
