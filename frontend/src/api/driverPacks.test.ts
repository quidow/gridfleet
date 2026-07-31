import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchDriverPackCatalog } from './driverPacks';
import { buildPlatformLabelMap } from '../hooks/useDriverPacks';
import type { DriverPack, DriverPackPlatform } from '../types/driverPacks';
import api from './client';

vi.mock('./client', () => ({
  default: { get: vi.fn() },
}));

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
    id: 'test_network',
    display_name: 'Test Network Platform',
    automation_name: 'TestAutomation',
    appium_platform_name: 'TestPlatform',
    device_types: ['real_device'],
    connection_types: ['network'],
    identity_scheme: 'test_serial',
    identity_scope: 'global',
    device_fields_schema: [],
    capabilities: {},
    ...overrides,
  };
}

describe('fetchDriverPackCatalog', () => {
  beforeEach(() => {
    (api.get as ReturnType<typeof vi.fn>).mockReset();
  });

  it('GETs /driver-packs/catalog and returns the packs array', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        packs: [
          {
            id: 'appium-uiautomator2',
            display_name: 'Appium UiAutomator2',
            state: 'enabled',
      active_runs: 0,
      live_sessions: 0,
            current_release: '2026.04.0',
          },
        ],
      },
    });
    const packs = await fetchDriverPackCatalog();
    expect(api.get).toHaveBeenCalledWith('/driver-packs/catalog');
    expect(packs).toHaveLength(1);
    expect(packs[0].id).toBe('appium-uiautomator2');
  });

  it('returns uploaded pack with display_name from catalog', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        packs: [
          {
            id: 'vendor/test-driver',
            display_name: 'Vendor Test Driver',
            state: 'enabled',
            active_runs: 0,
            live_sessions: 0,
            current_release: '2026.04.0',
            platforms: [
              {
                id: 'test_network',
                display_name: 'Test Network Platform',
                automation_name: 'TestAutomation',
                appium_platform_name: 'TestPlatform',
                device_types: ['real_device'],
                connection_types: ['network'],
                identity_scheme: 'test_serial',
                identity_scope: 'global',
                device_fields_schema: [
                  {
                    id: 'api_token',
                    label: 'API Token',
                    type: 'string',
                    required_for_session: true,
                    sensitive: true,
                  },
                ],
                capabilities: {},
              },
            ],
          },
        ],
      },
    });

    const packs = await fetchDriverPackCatalog();

    expect(packs).toHaveLength(1);
    const pack = packs[0];
    expect(pack.id).toBe('vendor/test-driver');
    expect(pack.display_name).toBe('Vendor Test Driver');
    expect(pack.state).toBe('enabled');
  });

  it('returns catalog with multiple driver packs', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        packs: [
          {
            id: 'appium-uiautomator2',
            display_name: 'Appium UiAutomator2',
            state: 'enabled',
            active_runs: 0,
            live_sessions: 0,
            current_release: '2026.04.0',
          },
          {
            id: 'vendor/test-driver',
            display_name: 'Vendor Test Driver',
            state: 'enabled',
            active_runs: 0,
            live_sessions: 0,
            current_release: '2026.04.0',
          },
        ],
      },
    });

    const packs = await fetchDriverPackCatalog();

    expect(packs).toHaveLength(2);
    expect(packs.map((p) => p.id)).toEqual(['appium-uiautomator2', 'vendor/test-driver']);
  });
});

describe('buildPlatformLabelMap with uploaded pack', () => {
  it('indexes uploaded pack platform label by pack:platform key', () => {
    const uploadedPack = makeDriverPack({
      id: 'vendor/test-driver',
      display_name: 'Vendor Test Driver',
      platforms: [makePlatform()],
    });

    const labels = buildPlatformLabelMap([uploadedPack]);

    expect(labels.get('vendor/test-driver:test_network')).toBe('Test Network Platform');
  });

  it('distinguishes packs with same platform id by pack-qualified key', () => {
    const firstPack = makeDriverPack({
      platforms: [
        makePlatform({
          id: 'android_mobile',
          display_name: 'Android Mobile (real device)',
          automation_name: 'UiAutomator2',
          appium_platform_name: 'Android',
          connection_types: ['usb'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
        }),
      ],
    });

    const uploadedPack = makeDriverPack({
      id: 'vendor/test-driver',
      display_name: 'Vendor Test Driver',
      platforms: [
        makePlatform({
          id: 'android_mobile',
          display_name: 'Uploaded Android Override',
          automation_name: 'CustomAuto',
          appium_platform_name: 'Android',
          connection_types: ['usb'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
        }),
      ],
    });

    const labels = buildPlatformLabelMap([firstPack, uploadedPack]);

    expect(labels.get('appium-uiautomator2:android_mobile')).toBe('Android Mobile (real device)');
    expect(labels.get('vendor/test-driver:android_mobile')).toBe('Uploaded Android Override');
  });

  it('uploaded pack display_name is returned separately from platform label', () => {
    const uploadedPack = makeDriverPack({
      id: 'vendor/test-driver',
      display_name: 'Vendor Test Driver',
      platforms: [makePlatform()],
    });

    // The pack-level display_name is separate from platform display_name
    expect(uploadedPack.display_name).toBe('Vendor Test Driver');

    const labels = buildPlatformLabelMap([uploadedPack]);
    // Platform label, not pack label
    expect(labels.get('vendor/test-driver:test_network')).toBe('Test Network Platform');
  });
});
