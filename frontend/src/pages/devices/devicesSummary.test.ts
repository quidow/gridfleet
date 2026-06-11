import { describe, expect, it } from 'vitest';
import type { DeviceRead } from '../../types';
import {
  buildDevicesSummaryHref,
  deriveDevicesSummaryStats,
  getAttentionHrefOptions,
  getAttentionTone,
} from './devicesSummary';

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  return {
    id: 'device-1',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    identity_scheme: 'adb_serial',
    identity_scope: 'global' as const,
    identity_value: 'serial-001',
    connection_target: 'serial-001',
    name: 'Pixel 8',
    manufacturer: null,
    model: null,
    os_version: '14',
    host_id: 'host-1',
    operational_state: 'available',
    needs_attention: false,
    tags: null,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
    battery_level_percent: 90,
    battery_temperature_c: 36,
    charging_state: 'charging',
    hardware_health_status: 'healthy',
    hardware_telemetry_reported_at: '2026-04-16T12:00:00Z',
    hardware_telemetry_state: 'fresh',
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
    emulator_state: null,
    blocked_reason: null,
    created_at: '2026-04-16T12:00:00Z',
    updated_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

describe('devicesSummary', () => {
  it('counts availability and attention totals', () => {
    const stats = deriveDevicesSummaryStats([
      makeDevice({ id: 'available', operational_state: 'available' }),
      makeDevice({ id: 'busy', operational_state: 'busy' }),
      makeDevice({ id: 'verifying', operational_state: 'verifying' }),
      makeDevice({ id: 'reserved', operational_state: 'available', is_reserved: true }),
      makeDevice({ id: 'offline', operational_state: 'offline' }),
      makeDevice({ id: 'maintenance', operational_state: 'maintenance' }),
      makeDevice({ id: 'attn', needs_attention: true }),
    ]);

    // Reservation is orthogonal to operational state: a reserved-but-idle
    // device still counts as available.
    expect(stats.available).toBe(3);
    expect(stats.busy).toBe(2);
    expect(stats.reserved).toBe(1);
    expect(stats.offline).toBe(1);
    expect(stats.maintenance).toBe(1);
    expect(stats.attentionCount).toBe(1);
  });

  it('attention tone is warn when any device needs attention', () => {
    const stats = deriveDevicesSummaryStats([makeDevice({ needs_attention: true })]);
    expect(getAttentionTone(stats)).toBe('warn');
  });

  it('attention tone is neutral when no devices need attention', () => {
    const stats = deriveDevicesSummaryStats([makeDevice({ needs_attention: false })]);
    expect(getAttentionTone(stats)).toBe('neutral');
  });

  it('attention href links to needs_attention=true', () => {
    expect(getAttentionHrefOptions()).toEqual({ needsAttention: true });
  });

  it('preserves unrelated query params when building summary hrefs', () => {
    const params = new URLSearchParams('platform_id=android_mobile&search=pixel&status=busy');
    expect(buildDevicesSummaryHref(params, { hardwareHealthStatus: 'warning' })).toBe(
      '/devices?platform_id=android_mobile&search=pixel&hardware_health_status=warning',
    );
  });

  it('emits status URL when option set', () => {
    const url = buildDevicesSummaryHref(new URLSearchParams(), { status: 'offline' });
    expect(url).toContain('status=offline');
  });

  it('emits needs_attention=true URL when option set', () => {
    const url = buildDevicesSummaryHref(new URLSearchParams(), { needsAttention: true });
    expect(url).toContain('needs_attention=true');
  });

  it('emits reserved=true URL when option set', () => {
    const url = buildDevicesSummaryHref(new URLSearchParams(), { reserved: true });
    expect(url).toContain('reserved=true');
  });

  it('clears the reserved param when building other summary hrefs', () => {
    const params = new URLSearchParams('reserved=true');
    expect(buildDevicesSummaryHref(params, { status: 'busy' })).toBe('/devices?status=busy');
  });
});

function fleet(): DeviceRead[] {
  const base = {
    operational_state: 'available',
    needs_attention: false,
    hardware_health_status: 'healthy',
    hardware_telemetry_state: 'fresh',
  } as unknown as DeviceRead;
  return [
    { ...base } as DeviceRead,
    { ...base, operational_state: 'busy' } as DeviceRead,
    { ...base, operational_state: 'offline', needs_attention: true } as DeviceRead,
    { ...base, operational_state: 'offline', needs_attention: true } as DeviceRead,
  ];
}

describe('deriveDevicesSummaryStats — independence from pagination', () => {
  it('reports counts across the entire input array, not a slice', () => {
    const stats = deriveDevicesSummaryStats(fleet());
    expect(stats.total).toBe(4);
    expect(stats.available).toBe(1);
    expect(stats.busy).toBe(1);
    expect(stats.offline).toBe(2);
    expect(stats.attentionCount).toBe(2);
  });
});
