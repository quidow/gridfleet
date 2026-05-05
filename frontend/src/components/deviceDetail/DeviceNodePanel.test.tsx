import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { DeviceDetail } from '../../types';
import DeviceNodePanel from './DeviceNodePanel';

const mutation = {
  isPending: false,
  variables: undefined,
  mutate: vi.fn(),
};

vi.mock('../../hooks/useDevices', () => ({
  useEnterDeviceMaintenance: () => mutation,
  useExitDeviceMaintenance: () => mutation,
  useRestartNode: () => mutation,
  useRunDeviceLifecycleAction: () => mutation,
  useStartNode: () => mutation,
  useStopNode: () => mutation,
  useToggleDeviceAutoManage: () => mutation,
}));

vi.mock('../../hooks/usePlatformDescriptor', () => ({
  usePlatformDescriptor: () => ({ lifecycleActions: [] }),
  platformDescriptorForDeviceType: (d: unknown) => d,
}));

function makeDevice(): DeviceDetail {
  return {
    id: 'device-1',
    pack_id: 'pack-1',
    platform_id: 'android',
    platform_label: 'Android',
    identity_scheme: 'serial',
    identity_scope: 'global',
    identity_value: 'abc123',
    connection_target: '192.168.1.254:5555',
    name: 'Pixel',
    os_version: '15',
    host_id: 'host-1',
    operational_state: 'available', hold: null,
    needs_attention: false,
    tags: null,
    manufacturer: 'Google',
    model: 'Pixel 9',
    model_number: null,
    software_versions: null,
    auto_manage: true,
    device_type: 'real_device',
    connection_type: 'network',
    ip_address: '192.168.1.254',
    device_config: null,
    battery_level_percent: null,
    battery_temperature_c: null,
    charging_state: null,
    hardware_health_status: 'unknown',
    hardware_telemetry_reported_at: null,
    hardware_telemetry_state: 'unknown',
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: null,
    reservation: null,
    lifecycle_policy_summary: {
      state: 'idle',
      label: 'Idle',
      detail: null,
      backoff_until: null,
    },
    health_summary: {
      healthy: true,
      summary: 'Healthy',
      last_checked_at: null,
    },
    emulator_state: null,
    created_at: '2026-04-28T12:00:00Z',
    updated_at: '2026-04-28T12:00:00Z',
    appium_node: {
      id: 'node-1',
      port: 4723,
      grid_url: 'http://selenium-hub:4444',
      pid: 36492,
      container_id: null,
      active_connection_target: '192.168.1.254:5555',
      state: 'running',
      started_at: '2026-04-28T13:51:00Z',
    },
    sessions: [],
  };
}

describe('DeviceNodePanel', () => {
  it('uses the shared justified definition list for node metadata rows', () => {
    render(<DeviceNodePanel device={makeDevice()} />);

    const row = screen.getByText('Port').closest('div');

    expect(row?.className).toContain('flex justify-between');
    expect(row?.className).not.toContain('grid-cols-[8rem,minmax(0,1fr)]');
  });
});
