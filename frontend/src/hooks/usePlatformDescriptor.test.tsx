import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import * as driverPacks from '../api/driverPacks';
import type { DriverPack, DriverPackPlatform, PlatformConnectionBehavior } from '../types';
import { findPlatformDescriptor, usePlatformDescriptor } from './usePlatformDescriptor';

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
    display_name: 'Android',
    automation_name: 'UiAutomator2',
    appium_platform_name: 'Android',
    device_types: ['real_device'],
    connection_types: ['usb', 'network'],
    identity_scheme: 'android_serial',
    identity_scope: 'host',
    device_fields_schema: [],
    capabilities: {},
    ...overrides,
  };
}

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

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

it('returns descriptor for known platform (legacy 1-arg)', async () => {
  vi.spyOn(driverPacks, 'fetchDriverPackCatalog').mockResolvedValue([
    makeDriverPack({
      insecure_features: [],
      platforms: [
        makePlatform({
          display_metadata: { icon_kind: 'mobile' },
          default_capabilities: {},
          connection_behavior: makeConnectionBehavior(),
        }),
      ],
    }),
  ]);

  const { result } = renderHook(() => usePlatformDescriptor('android_mobile'), {
    wrapper: makeWrapper(),
  });

  await waitFor(() => expect(result.current).not.toBeNull());
  expect(result.current?.iconKind).toBe('mobile');
  expect(result.current?.appiumPlatformName).toBe('Android');
  expect(result.current?.deviceTypes).toEqual(['real_device']);
  expect(result.current?.connectionBehavior).toEqual(makeConnectionBehavior());
});

it('returns the descriptor for the requested pack and platform', () => {
  const descriptor = findPlatformDescriptor(
    [
      makeDriverPack({
        id: 'pack-a',
        display_name: 'Pack A',
        platforms: [
          makePlatform({
            id: 'android_real',
            display_name: 'A Android',
            connection_types: ['usb'],
            identity_scheme: 'serial',
          }),
        ],
      }),
      makeDriverPack({
        id: 'pack-b',
        display_name: 'Pack B',
        platforms: [
          makePlatform({
            id: 'android_real',
            display_name: 'B Android',
            connection_types: ['network'],
            identity_scheme: 'serial',
          }),
        ],
      }),
    ],
    'pack-b',
    'android_real',
  );

  expect(descriptor?.displayName).toBe('B Android');
  expect(descriptor?.connectionTypes).toEqual(['network']);
});

it('returns null when platform unknown', async () => {
  vi.spyOn(driverPacks, 'fetchDriverPackCatalog').mockResolvedValue([]);

  const { result } = renderHook(() => usePlatformDescriptor('unknown'), {
    wrapper: makeWrapper(),
  });

  await waitFor(() => expect(result.current).toBeNull());
});
