import { describe, expect, it } from 'vitest';
import {
  managedDeviceConfigKeys,
  omitManagedDeviceConfig,
  restoreManagedDeviceConfig,
} from './utils';

describe('DeviceConfigEditor config projection', () => {
  it('omits manifest-managed fields from editable JSON and restores them on save', () => {
    const managedKeys = managedDeviceConfigKeys([
      { id: 'roku_password' },
      { id: 'pin' },
    ]);
    const sourceConfig = {
      roku_password: '********',
      pin: '2468',
      custom_timeout: 30,
    };

    expect(omitManagedDeviceConfig(sourceConfig, managedKeys)).toEqual({ custom_timeout: 30 });
    expect(restoreManagedDeviceConfig({ custom_timeout: 60 }, sourceConfig, managedKeys)).toEqual({
      roku_password: '********',
      pin: '2468',
      custom_timeout: 60,
    });
  });
});
