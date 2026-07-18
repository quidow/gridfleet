import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DeviceEditModal } from './DeviceEditModal';
import type { DeviceRead } from '../../types';

const mockUseDriverPackCatalog = vi.fn();

vi.mock('../../hooks/useDevices', () => ({
  useUpdateDevice: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock('../../hooks/useDriverPacks', () => ({
  useDriverPackCatalog: () => mockUseDriverPackCatalog(),
}));

function xcuitestCatalog() {
  return {
    data: [
      {
        id: 'appium-xcuitest',
        display_name: 'Appium XCUITest',
        state: 'enabled',
        current_release: '2026.04.0',
        runtime_policy: { strategy: 'recommended' },
        active_runs: 0,
        live_sessions: 0,
        platforms: [
          {
            id: 'tvos',
            display_name: 'tvOS',
            automation_name: 'XCUITest',
            appium_platform_name: 'tvOS',
            device_types: ['real_device', 'simulator'],
            connection_types: ['usb', 'network', 'virtual'],
            identity_scheme: 'apple_udid',
            identity_scope: 'global',
            discovery_kind: 'apple',
            lifecycle_actions: [{ id: 'reconnect' }],
            device_fields_schema: [],
            capabilities: {},
            display_metadata: { icon_kind: 'tv' },
            default_capabilities: {},
            connection_behavior: {
              default_device_type: 'real_device',
              default_connection_type: 'usb',
              requires_connection_target: true,
              requires_ip_address: false,
            },
            device_type_overrides: {
              real_device: {
                device_fields_schema: [
                  {
                    id: 'wda_base_url',
                    label: 'WDA base URL',
                    type: 'network_endpoint',
                    required_for_session: true,
                    capability_name: 'appium:wdaBaseUrl',
                  },
                  {
                    id: 'use_preinstalled_wda',
                    label: 'Use pre-installed WDA',
                    type: 'bool',
                    default: true,
                    capability_name: 'appium:usePreinstalledWDA',
                  },
                ],
              },
            },
          },
        ],
      },
    ],
  };
}

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  return {
    id: 'device-1',
    pack_id: 'appium-xcuitest',
    platform_id: 'tvos',
    platform_label: null,
    identity_scheme: 'apple_udid',
    identity_scope: 'global' as const,
    identity_value: 'APPLE-TV-UDID',
    connection_target: 'APPLE-TV-UDID',
    name: 'Apple TV',
    manufacturer: null,
    model: null,
    os_version: '17.4',
    host_id: 'host-1',
    operational_state: 'available',
    needs_attention: false,
    is_reserved: false,
    tags: null,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
    battery_level_percent: null,
    battery_temperature_c: null,
    charging_state: null,
    hardware_health_status: null,
    hardware_telemetry_reported_at: null,
    hardware_telemetry_state: null,
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: '2026-04-16T12:00:00Z',
    reservation: null,
    lifecycle_policy_summary: {
      state: 'idle',
      label: 'Idle',
      detail: null,
      backoff_until: null,
    },
    health_summary: {
      device: { status: 'ok', detail: null, checked_at: null },
      node: { status: 'ok', detail: 'running', checked_at: null },
      viability: { status: 'ok', detail: 'passed', checked_at: null },
      overall: 'ok',
    },
    created_at: '2026-04-16T12:00:00Z',
    updated_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

describe('DeviceEditModal manifest fields', () => {
  beforeEach(() => {
    mockUseDriverPackCatalog.mockReturnValue(xcuitestCatalog());
  });

  afterEach(() => {
    mockUseDriverPackCatalog.mockReset();
  });

  it('renders device-type override manifest fields for a real device', () => {
    render(
      <DeviceEditModal
        device={makeDevice()}
        hostMap={new Map([['host-1', 'Host 1']])}
        onClose={vi.fn()}
        onRequestVerification={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('WDA base URL')).toBeInTheDocument();
    expect(screen.getByLabelText('Use pre-installed WDA')).toBeInTheDocument();
  });
});
