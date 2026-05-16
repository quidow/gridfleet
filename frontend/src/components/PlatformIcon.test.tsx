import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';
import * as driverPacks from '../api/driverPacks';
import { PlatformIcon } from './PlatformIcon';

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

it('renders TV icon when iconKind is tv', async () => {
  vi.spyOn(driverPacks, 'fetchDriverPackCatalog').mockResolvedValue([
    {
      id: 'appium-uiautomator2',
      display_name: 'Appium UiAutomator2',
      enabled: true,
      current_release: '2026.04.0',
      insecure_features: [],
      platforms: [
        {
          id: 'android_tv',
          display_name: 'Android TV',
          automation_name: 'UiAutomator2',
          appium_platform_name: 'Android',
          device_types: ['real_device'],
          connection_types: ['network'],
          grid_slots: ['native'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          discovery_kind: 'adb',
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'tv' },
          default_capabilities: {},
        },
      ],
    },
  ]);

  render(<PlatformIcon platformId="android_tv" />, { wrapper: makeWrapper() });

  expect(await screen.findByTestId('platform-icon-tv')).toBeInTheDocument();
});

it('renders TV icon for set-top platforms', async () => {
  vi.spyOn(driverPacks, 'fetchDriverPackCatalog').mockResolvedValue([
    {
      id: 'appium-roku-dlenroc',
      display_name: 'Roku',
      enabled: true,
      current_release: '2026.04.5',
      insecure_features: [],
      platforms: [
        {
          id: 'roku_network',
          display_name: 'Roku',
          automation_name: 'Roku',
          appium_platform_name: 'roku',
          device_types: ['real_device'],
          connection_types: ['network'],
          grid_slots: ['native'],
          identity_scheme: 'roku_serial',
          identity_scope: 'global',
          discovery_kind: 'network_endpoint',
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'set_top' },
          default_capabilities: {},
        },
      ],
    },
  ]);

  render(<PlatformIcon platformId="roku_network" />, { wrapper: makeWrapper() });

  expect(await screen.findByTestId('platform-icon-tv')).toBeInTheDocument();
});

it('uses different deterministic colors for different platforms with the same icon kind', async () => {
  vi.spyOn(driverPacks, 'fetchDriverPackCatalog').mockResolvedValue([
    {
      id: 'appium-uiautomator2',
      display_name: 'Appium UiAutomator2',
      enabled: true,
      current_release: '2026.04.4',
      insecure_features: [],
      platforms: [
        {
          id: 'firetv_real',
          display_name: 'Fire TV (real device)',
          automation_name: 'UiAutomator2',
          appium_platform_name: 'Android',
          device_types: ['real_device'],
          connection_types: ['network'],
          grid_slots: ['native'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          discovery_kind: 'adb',
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'tv' },
          default_capabilities: {},
        },
      ],
    },
    {
      id: 'appium-roku-dlenroc',
      display_name: 'Roku',
      enabled: true,
      current_release: '2026.04.5',
      insecure_features: [],
      platforms: [
        {
          id: 'roku_network',
          display_name: 'Roku (network)',
          automation_name: 'Roku',
          appium_platform_name: 'roku',
          device_types: ['real_device'],
          connection_types: ['network'],
          grid_slots: ['native'],
          identity_scheme: 'roku_serial',
          identity_scope: 'global',
          discovery_kind: 'network_endpoint',
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'set_top' },
          default_capabilities: {},
        },
      ],
    },
  ]);

  render(
    <div>
      <PlatformIcon platformId="firetv_real" />
      <PlatformIcon platformId="roku_network" />
    </div>,
    { wrapper: makeWrapper() },
  );
  const icons = await screen.findAllByTestId('platform-icon-tv');

  expect(screen.getByText('Fire TV')).toBeInTheDocument();
  expect(screen.getByText('Roku')).toBeInTheDocument();
  expect(icons[0].className).not.toBe(icons[1].className);
});
