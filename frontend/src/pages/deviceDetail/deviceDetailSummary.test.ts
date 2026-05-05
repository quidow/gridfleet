import { describe, expect, it } from 'vitest';
import type { DeviceDetail } from '../../types';
import {
  getDeviceDetailStatusPills,
  hardwareSummary,
} from './deviceDetailSummary';

function makeDevice(overrides: Partial<DeviceDetail> = {}): DeviceDetail {
  return {
    id: 'device-1',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    identity_scheme: 'adb_serial',
    identity_scope: 'global' as const,
    identity_value: 'device-1',
    connection_target: '10.0.0.50:5555',
    name: 'Lab Pixel',
    manufacturer: null,
    model: null,
    needs_attention: false,
    os_version: '14',
    host_id: 'host-1',
    operational_state: 'available', hold: null,
    tags: null,
    auto_manage: true,
    device_type: 'real_device',
    connection_type: 'network',
    ip_address: '10.0.0.50',
    battery_level_percent: 84,
    battery_temperature_c: 36.7,
    charging_state: 'charging',
    hardware_health_status: 'healthy',
    hardware_telemetry_reported_at: '2026-03-30T10:00:03Z',
    hardware_telemetry_state: 'fresh',
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: '2026-03-30T10:00:03Z',
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
      last_checked_at: '2026-03-30T10:00:03Z',
    },
    emulator_state: null,
    created_at: '2026-03-30T10:00:03Z',
    updated_at: '2026-03-30T10:00:03Z',
    appium_node: null,
    sessions: [],
    ...overrides,
  };
}

describe('deviceDetailSummary', () => {
  it('keeps unsupported hardware neutral and non-clickable', () => {
    const device = makeDevice({
      hardware_health_status: 'healthy',
      hardware_telemetry_state: 'unsupported',
      hardware_telemetry_reported_at: null,
    });

    expect(hardwareSummary(device)).toMatchObject({
      value: 'No telemetry',
      tone: 'neutral',
    });
    expect(hardwareSummary(device).to).toBeUndefined();
  });

  it('builds hardware and connectivity pills (no lifecycle/readiness)', () => {
    const device = makeDevice({
      operational_state: 'busy', hold: null,
      hardware_health_status: 'warning',
      health_summary: {
        healthy: false,
        summary: 'ADB not responsive',
        last_checked_at: '2026-03-30T10:00:03Z',
      },
    });

    const pills = getDeviceDetailStatusPills(device);

    expect(pills).toHaveLength(2);
    expect(pills[0]).toMatchObject({
      key: 'hardware',
      label: 'Hardware',
      tone: 'warn',
      to: '/devices?hardware_health_status=warning',
    });
    expect(pills[1]).toMatchObject({
      key: 'connectivity',
      label: 'Connectivity',
      value: 'ADB not responsive',
      tone: 'error',
      to: `/devices/${device.id}?tab=triage#device-health`,
    });
  });
});
