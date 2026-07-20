import { describe, expect, it } from 'vitest';
import type { DeviceDetail } from '../../types';
import { getDeviceDetailStatusPills } from './deviceDetailSummary';

const healthSummary = (over: Partial<DeviceDetail['health_summary']> = {}): DeviceDetail['health_summary'] => ({
  device: { status: 'ok', detail: null, checked_at: '2026-03-30T10:00:03Z' },
  node: { status: 'ok', detail: 'running', checked_at: null },
  viability: { status: 'unknown', detail: 'not run', checked_at: null },
  overall: 'ok',
  ...over,
});

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
    operational_state: 'available',
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
    health_summary: healthSummary(),
    created_at: '2026-03-30T10:00:03Z',
    updated_at: '2026-03-30T10:00:03Z',
    appium_node: null,
    sessions: [],
    ...overrides,
  };
}

describe('deviceDetailSummary', () => {
  it('builds the three verdict pills', () => {
    const device = makeDevice({
      operational_state: 'busy',
      health_summary: healthSummary({
        device: { status: 'failed', detail: 'ADB not responsive', checked_at: '2026-03-30T10:00:03Z' },
        node: { status: 'warn', detail: 'starting', checked_at: null },
        viability: { status: 'unknown', detail: 'not run', checked_at: null },
        overall: 'failed',
      }),
    });

    const pills = getDeviceDetailStatusPills(device);

    expect(pills).toHaveLength(3);
    expect(pills[0]).toMatchObject({
      key: 'device',
      label: 'Device',
      value: 'ADB not responsive',
      tone: 'error',
      to: `/devices/${device.id}?tab=triage#device-health`,
    });
    expect(pills[1]).toMatchObject({
      key: 'node',
      label: 'Node',
      value: 'starting',
      tone: 'warn',
    });
    expect(pills[2]).toMatchObject({
      key: 'viability',
      label: 'Viability',
      value: 'not run',
      tone: 'neutral',
    });
  });

  it('maps verdict statuses to tones and falls back to status labels without detail', () => {
    const pills = getDeviceDetailStatusPills(
      makeDevice({
        health_summary: healthSummary({
          device: { status: 'ok', detail: null, checked_at: null },
          node: { status: 'unknown', detail: null, checked_at: null },
          viability: { status: 'failed', detail: null, checked_at: null },
          overall: 'failed',
        }),
      }),
    );

    expect(pills[0]).toMatchObject({ key: 'device', value: 'OK', tone: 'ok' });
    expect(pills[1]).toMatchObject({ key: 'node', value: 'Unknown', tone: 'neutral' });
    expect(pills[2]).toMatchObject({ key: 'viability', value: 'Failed', tone: 'error' });
  });
});
