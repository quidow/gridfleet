import { describe, expect, it } from 'vitest';
import { findPlatformDescriptorByKey, makePlatformKey, parsePlatformKey } from './platformSelection';
import type { DriverPack, PlatformConnectionBehavior } from '../types/driverPacks';

function makeConnectionBehavior(
  overrides: Partial<PlatformConnectionBehavior> = {},
): PlatformConnectionBehavior {
  return {
    allow_transport_identity_until_host_resolution: false,
    requires_connection_target: true,
    requires_ip_address: false,
    ...overrides,
  };
}

const packs = [
  {
    id: 'appium-uiautomator2',
    display_name: 'Built in',
    state: 'enabled',
    current_release: '1.0.0',
    license: 'Apache-2.0',
    maintainer: 'gridfleet-team',
    platforms: [
      {
        id: 'android_mobile',
        display_name: 'Android Builtin',
        automation_name: 'UiAutomator2',
        appium_platform_name: 'Android',
        device_types: ['real_device'],
        connection_types: ['usb'],
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        lifecycle_actions: [{ id: 'reconnect' }],
        device_fields_schema: [],
        capabilities: {},
        display_metadata: { icon_kind: 'mobile' },
        default_capabilities: {},
        connection_behavior: makeConnectionBehavior(),
      },
    ],
    runtime_policy: { strategy: 'recommended' },
    active_runs: 0,
    live_sessions: 0,
  },
  {
    id: 'local/uiautomator2-android-real',
    display_name: 'Local fork',
    state: 'enabled',
    current_release: '1.0.0',
    license: 'Apache-2.0',
    maintainer: 'gridfleet-team',
    platforms: [
      {
        id: 'android_mobile',
        display_name: 'Android Local',
        automation_name: 'UiAutomator2',
        appium_platform_name: 'Android',
        device_types: ['real_device'],
        connection_types: ['network'],
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        device_fields_schema: [],
        capabilities: {},
        display_metadata: { icon_kind: 'mobile' },
        default_capabilities: {},
        connection_behavior: makeConnectionBehavior({ requires_ip_address: true }),
      },
    ],
    runtime_policy: { strategy: 'recommended' },
    active_runs: 0,
    live_sessions: 0,
  },
] satisfies DriverPack[];

describe('platformSelection', () => {
  it('round-trips composite keys', () => {
    const key = makePlatformKey('local/uiautomator2-android-real', 'android_mobile');
    expect(parsePlatformKey(key)).toEqual({
      packId: 'local/uiautomator2-android-real',
      platformId: 'android_mobile',
    });
  });

  it('rejects malformed composite keys', () => {
    expect(parsePlatformKey('')).toBeNull();
    expect(parsePlatformKey('pack-only')).toBeNull();
    expect(parsePlatformKey('::platform')).toBeNull();
    expect(parsePlatformKey('pack::')).toBeNull();
    expect(parsePlatformKey('pack::platform::extra')).toBeNull();
  });

  it('keeps platforms with the same platform_id distinct by pack_id', () => {
    const descriptor = findPlatformDescriptorByKey(
      packs,
      makePlatformKey('local/uiautomator2-android-real', 'android_mobile'),
    );
    expect(descriptor?.packId).toBe('local/uiautomator2-android-real');
    expect(descriptor?.displayName).toBe('Android Local');
    expect(descriptor?.connectionBehavior.requires_ip_address).toBe(true);
    expect(descriptor?.identityScheme).toBe('android_serial');
    expect(descriptor?.identityScope).toBe('host');
    expect(descriptor?.lifecycleActions).toEqual([]);
  });

  it('copies identity and lifecycle metadata into descriptors', () => {
    const descriptor = findPlatformDescriptorByKey(
      packs,
      makePlatformKey('appium-uiautomator2', 'android_mobile'),
    );

    expect(descriptor?.identityScheme).toBe('android_serial');
    expect(descriptor?.identityScope).toBe('host');
    expect(descriptor?.lifecycleActions).toEqual(['reconnect']);
  });
});
