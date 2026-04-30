import { describe, expect, it } from 'vitest';
import {
  buildDeviceFieldLabelMap,
  deviceUpdateRequiresReverification,
  missingSetupFieldLabel,
  readinessLabel,
} from './readiness';
import type { DevicePatch, DeviceRead } from '../types';

describe('readinessLabel', () => {
  it('maps each readiness state to a human-readable label', () => {
    expect(readinessLabel('setup_required')).toBe('Setup Required');
    expect(readinessLabel('verification_required')).toBe('Needs Verification');
    expect(readinessLabel('verified')).toBe('Verified');
  });
});

describe('missingSetupFieldLabel', () => {
  it('returns catalog label when available', () => {
    const labels = new Map([['ip_address', 'IP Address'], ['roku_dev_password', 'Roku Developer Password']]);
    expect(missingSetupFieldLabel('ip_address', labels)).toBe('IP Address');
    expect(missingSetupFieldLabel('roku_dev_password', labels)).toBe('Roku Developer Password');
  });

  it('falls back to humanized field name', () => {
    expect(missingSetupFieldLabel('ip_address')).toBe('ip address');
    expect(missingSetupFieldLabel('some_custom_field')).toBe('some custom field');
  });
});

describe('buildDeviceFieldLabelMap', () => {
  it('builds map from field schema', () => {
    const fields = [
      { id: 'ip_address', label: 'IP Address' },
      { id: 'roku_dev_password', label: 'Roku Developer Password' },
    ];
    const map = buildDeviceFieldLabelMap(fields);
    expect(map.get('ip_address')).toBe('IP Address');
    expect(map.get('roku_dev_password')).toBe('Roku Developer Password');
  });
});

describe('deviceUpdateRequiresReverification', () => {
  const baseDevice = {
    connection_target: 'serial-001',
    ip_address: '192.168.1.10',
  } as unknown as DeviceRead;

  it('triggers when connection_target changes', () => {
    expect(deviceUpdateRequiresReverification(baseDevice, { connection_target: 'new-serial' })).toBe(true);
  });

  it('triggers when ip_address changes', () => {
    expect(deviceUpdateRequiresReverification(baseDevice, { ip_address: '10.0.0.1' })).toBe(true);
  });

  it('does not trigger when unrelated field changes', () => {
    expect(deviceUpdateRequiresReverification(baseDevice, { name: 'New Name' } as DevicePatch)).toBe(false);
  });

  it('triggers on custom readinessFields', () => {
    expect(
      deviceUpdateRequiresReverification(
        { ...baseDevice, device_config: { roku_dev_password: 'old' } } as unknown as DeviceRead,
        { device_config: { roku_dev_password: 'new' } } as DevicePatch,
        ['roku_dev_password'],
      ),
    ).toBe(true);
  });

  it('triggers when readinessField is set for first time', () => {
    expect(
      deviceUpdateRequiresReverification(
        baseDevice,
        { device_config: { custom_field: 'value' } } as DevicePatch,
        ['custom_field'],
      ),
    ).toBe(true);
  });

  it('does not treat readinessFields as top-level legacy patch fields', () => {
    expect(
      deviceUpdateRequiresReverification(
        { ...baseDevice, device_config: { roku_dev_password: 'old' } } as unknown as DeviceRead,
        { name: 'New Name' } as DevicePatch,
        ['roku_dev_password'],
      ),
    ).toBe(false);
  });
});
