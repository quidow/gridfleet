import { describe, expect, it } from 'vitest';
import { buildPlatformLabelMap, buildPlatformIdLabelMap } from './useDriverPacks';

describe('buildPlatformLabelMap', () => {
  it('indexes labels by pack and platform id', () => {
    const labels = buildPlatformLabelMap([
      {
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        enabled: true,
        current_release: '2026.04.0',
        platforms: [
          {
            id: 'android_mobile',
            display_name: 'Android (real device)',
            automation_name: 'UiAutomator2',
            appium_platform_name: 'Android',
            device_types: ['real_device'],
            connection_types: ['usb'],
            grid_slots: ['native'],
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            discovery_kind: 'adb',
            device_fields_schema: [],
            capabilities: {},
          },
        ],
      },
    ]);

    expect(labels.get('appium-uiautomator2:android_mobile')).toBe('Android (real device)');
  });
});

describe('buildPlatformIdLabelMap', () => {
  it('indexes labels by platform id only', () => {
    const labels = buildPlatformIdLabelMap([
      {
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        enabled: true,
        current_release: '2026.04.0',
        platforms: [
          {
            id: 'android_mobile',
            display_name: 'Android Mobile',
            automation_name: 'UiAutomator2',
            appium_platform_name: 'Android',
            device_types: ['real_device'],
            connection_types: ['usb'],
            grid_slots: ['native'],
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            discovery_kind: 'adb',
            device_fields_schema: [],
            capabilities: {},
          },
        ],
      },
    ]);

    expect(labels.get('android_mobile')).toBe('Android Mobile');
    expect(labels.has('appium-uiautomator2:android_mobile')).toBe(false);
  });

  it('first pack wins on platform id collision', () => {
    const labels = buildPlatformIdLabelMap([
      {
        id: 'pack-a',
        display_name: 'Pack A',
        enabled: true,
        current_release: '1.0',
        platforms: [{ id: 'shared_platform', display_name: 'First Label', automation_name: '', appium_platform_name: '', device_types: [], connection_types: [], grid_slots: [], identity_scheme: '', identity_scope: 'host', discovery_kind: 'adb', device_fields_schema: [], capabilities: {} }],
      },
      {
        id: 'pack-b',
        display_name: 'Pack B',
        enabled: true,
        current_release: '1.0',
        platforms: [{ id: 'shared_platform', display_name: 'Second Label', automation_name: '', appium_platform_name: '', device_types: [], connection_types: [], grid_slots: [], identity_scheme: '', identity_scope: 'host', discovery_kind: 'adb', device_fields_schema: [], capabilities: {} }],
      },
    ]);

    expect(labels.get('shared_platform')).toBe('First Label');
  });
});
