import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import * as driverPacks from '../api/driverPacks';
import type { DriverPack } from '../types';
import { findPlatformDescriptor, usePlatformDescriptor } from './usePlatformDescriptor';

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
    {
      id: 'appium-uiautomator2',
      display_name: 'Appium UiAutomator2',
      enabled: true,
      current_release: '2026.04.0',
      insecure_features: [],
      platforms: [
        {
          id: 'android_mobile',
          display_name: 'Android',
          automation_name: 'UiAutomator2',
          appium_platform_name: 'Android',
          device_types: ['real_device'],
          connection_types: ['usb', 'network'],
          grid_slots: ['native'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          discovery_kind: 'adb',
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'mobile' },
          default_capabilities: {},
          connection_behavior: {},
        },
      ],
    },
  ]);

  const { result } = renderHook(() => usePlatformDescriptor('android_mobile'), {
    wrapper: makeWrapper(),
  });

  await waitFor(() => expect(result.current).not.toBeNull());
  expect(result.current?.iconKind).toBe('mobile');
  expect(result.current?.appiumPlatformName).toBe('Android');
  expect(result.current?.deviceTypes).toEqual(['real_device']);
  expect(result.current?.connectionBehavior).toEqual({});
});

it('returns the descriptor for the requested pack and platform', () => {
  const descriptor = findPlatformDescriptor(
    [
      {
        id: 'pack-a',
        display_name: 'Pack A',
        platforms: [
          {
            id: 'android_real',
            display_name: 'A Android',
            automation_name: 'UiAutomator2',
            appium_platform_name: 'Android',
            device_types: ['real_device'],
            connection_types: ['usb'],
            grid_slots: ['native'],
            identity_scheme: 'serial',
            identity_scope: 'host',
            discovery_kind: 'adb',
            device_fields_schema: [],
            capabilities: {},
          },
        ],
      },
      {
        id: 'pack-b',
        display_name: 'Pack B',
        platforms: [
          {
            id: 'android_real',
            display_name: 'B Android',
            automation_name: 'UiAutomator2',
            appium_platform_name: 'Android',
            device_types: ['real_device'],
            connection_types: ['network'],
            grid_slots: ['native'],
            identity_scheme: 'serial',
            identity_scope: 'host',
            discovery_kind: 'adb',
            device_fields_schema: [],
            capabilities: {},
          },
        ],
      },
    ] as DriverPack[],
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
