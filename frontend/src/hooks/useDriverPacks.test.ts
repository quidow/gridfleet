import { describe, expect, it } from 'vitest';
import { buildPlatformLabelMap, buildPlatformIdLabelMap } from './useDriverPacks';
import type { DriverPack, DriverPackPlatform } from '../types/driverPacks';

function makeDriverPack(overrides: Partial<DriverPack> = {}): DriverPack {
  return {
    id: 'appium-uiautomator2',
    display_name: 'Appium UiAutomator2',
    maintainer: 'gridfleet-team',
    license: 'Apache-2.0',
    state: 'enabled',
    active_runs: 0,
    live_sessions: 0,
    current_release: '2026.04.0',
    runtime_policy: { strategy: 'recommended' },
    ...overrides,
  };
}

function makePlatform(overrides: Partial<DriverPackPlatform> = {}): DriverPackPlatform {
  return {
    id: 'android_mobile',
    display_name: 'Android (real device)',
    automation_name: 'UiAutomator2',
    appium_platform_name: 'Android',
    device_types: ['real_device'],
    connection_types: ['usb'],
    identity_scheme: 'android_serial',
    identity_scope: 'host',
    device_fields_schema: [],
    capabilities: {},
    ...overrides,
  };
}

describe('buildPlatformLabelMap', () => {
  it('indexes labels by pack and platform id', () => {
    const labels = buildPlatformLabelMap([
      makeDriverPack({
        platforms: [makePlatform()],
      }),
    ]);

    expect(labels.get('appium-uiautomator2:android_mobile')).toBe('Android (real device)');
  });
});

describe('buildPlatformIdLabelMap', () => {
  it('indexes labels by platform id only', () => {
    const labels = buildPlatformIdLabelMap([
      makeDriverPack({
        platforms: [makePlatform({ display_name: 'Android Mobile' })],
      }),
    ]);

    expect(labels.get('android_mobile')).toBe('Android Mobile');
    expect(labels.has('appium-uiautomator2:android_mobile')).toBe(false);
  });

  it('first pack wins on platform id collision', () => {
    const labels = buildPlatformIdLabelMap([
      makeDriverPack({
        id: 'pack-a',
        display_name: 'Pack A',
        current_release: '1.0',
        platforms: [
          makePlatform({
            id: 'shared_platform',
            display_name: 'First Label',
            automation_name: '',
            appium_platform_name: '',
            device_types: [],
            connection_types: [],
            identity_scheme: '',
          }),
        ],
      }),
      makeDriverPack({
        id: 'pack-b',
        display_name: 'Pack B',
        current_release: '1.0',
        platforms: [
          makePlatform({
            id: 'shared_platform',
            display_name: 'Second Label',
            automation_name: '',
            appium_platform_name: '',
            device_types: [],
            connection_types: [],
            identity_scheme: '',
          }),
        ],
      }),
    ]);

    expect(labels.get('shared_platform')).toBe('First Label');
  });
});
