import { describe, expect, it } from 'vitest';
import type { DeviceRead } from '../../types';
import { deriveUnifiedHealth } from '../../lib/deviceUnifiedHealth';

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  return {
    id: `device-${Math.random().toString(36).slice(2)}`,
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
    needs_attention: false,
    os_version: '14',
    host_id: 'host-1',
    operational_state: 'available', hold: null,
    tags: null,
    auto_manage: true,
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
      healthy: true,
      summary: 'Healthy',
      last_checked_at: '2026-04-16T12:00:00Z',
    },
    emulator_state: null,
    created_at: '2026-04-16T12:00:00Z',
    updated_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

describe('deriveUnifiedHealth', () => {
  it('returns ok when liveness, hardware, and telemetry are all healthy', () => {
    expect(deriveUnifiedHealth(makeDevice())).toEqual({
      tone: 'ok',
      label: 'Healthy',
      reasons: ['Healthy'],
      summary: 'Healthy',
    });
  });

  it('returns error when liveness is false even with healthy hardware', () => {
    const device = makeDevice({
      health_summary: {
        healthy: false,
        summary: 'Grid node offline',
        last_checked_at: '2026-04-16T12:00:00Z',
      },
      hardware_health_status: 'healthy',
      hardware_telemetry_state: 'fresh',
    });

    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.label).toBe('Unhealthy');
    expect(result.reasons).toEqual(['Grid node offline']);
  });

  it('returns error when hardware is critical, even if liveness is healthy', () => {
    const device = makeDevice({ hardware_health_status: 'critical' });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.label).toBe('Unhealthy');
    expect(result.reasons).toEqual(['Hardware critical']);
  });

  it('returns warn when hardware is warning and liveness is healthy', () => {
    const device = makeDevice({ hardware_health_status: 'warning' });
    expect(deriveUnifiedHealth(device).tone).toBe('warn');
  });

  it('returns warn when telemetry is stale and liveness is healthy', () => {
    const device = makeDevice({ hardware_telemetry_state: 'stale' });
    expect(deriveUnifiedHealth(device).reasons).toContain('Telemetry stale');
  });

  it('does not downgrade to warn when telemetry is unsupported', () => {
    const device = makeDevice({
      hardware_health_status: 'unknown',
      hardware_telemetry_state: 'unsupported',
    });
    expect(deriveUnifiedHealth(device).tone).toBe('ok');
  });

  it('returns unknown when liveness is null and hardware is unknown', () => {
    const device = makeDevice({
      health_summary: {
        healthy: null,
        summary: '',
        last_checked_at: null,
      },
      hardware_health_status: 'unknown',
      hardware_telemetry_state: 'unknown',
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('unknown');
    expect(result.label).toBe('Unknown');
  });

  it('returns unknown health when data cannot be sampled', () => {
    const device = makeDevice({
      health_summary: {
        healthy: null,
        summary: '',
        last_checked_at: null,
      },
      hardware_health_status: 'unknown',
      hardware_telemetry_state: 'unsupported',
    });
    expect(deriveUnifiedHealth(device).reasons).toEqual(['Health unknown']);
  });

  it('prioritises liveness error over stale telemetry', () => {
    const device = makeDevice({
      health_summary: {
        healthy: false,
        summary: 'Heartbeat missed',
        last_checked_at: '2026-04-16T12:00:00Z',
      },
      hardware_telemetry_state: 'stale',
    });
    expect(deriveUnifiedHealth(device).tone).toBe('error');
  });

  it('joins multiple warn reasons into summary', () => {
    const device = makeDevice({
      hardware_health_status: 'warning',
      hardware_telemetry_state: 'stale',
    });
    expect(deriveUnifiedHealth(device).summary).toBe('Hardware warning · Telemetry stale');
  });

  it('returns ok when liveness is true and hardware is unknown but telemetry fresh', () => {
    const device = makeDevice({ hardware_health_status: 'unknown' });
    expect(deriveUnifiedHealth(device).tone).toBe('ok');
  });

  it('returns error when lifecycle is suppressed even with healthy probe', () => {
    const device = makeDevice({
      lifecycle_policy_summary: {
        state: 'suppressed',
        label: 'Suppressed',
        detail: 'Node restart failed',
        backoff_until: null,
      },
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.reasons).toContain('Node restart failed');
  });

  it('surfaces lifecycle backoff detail as operator-visible warning', () => {
    const device = makeDevice({
      lifecycle_policy_summary: {
        state: 'backoff',
        label: 'Backing Off',
        detail: 'Agent failed to start node: port occupied',
        backoff_until: '2026-04-16T12:01:00Z',
      },
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('warn');
    expect(result.reasons).toContain('Agent failed to start node: port occupied');
  });

  it('returns error when lifecycle is manual', () => {
    const device = makeDevice({
      lifecycle_policy_summary: {
        state: 'manual',
        label: 'Manual',
        detail: null,
        backoff_until: null,
      },
    });
    expect(deriveUnifiedHealth(device).reasons).toContain('Manual recovery requested');
  });

  it('returns warn when readiness is setup_required and probe is healthy', () => {
    const device = makeDevice({
      readiness_state: 'setup_required',
      missing_setup_fields: ['manufacturer'],
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('warn');
    expect(result.reasons).toContain('Setup required');
  });

  it('returns warn when readiness is verification_required and probe is healthy', () => {
    const device = makeDevice({ readiness_state: 'verification_required' });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('warn');
    expect(result.reasons).toContain('Pending verification');
  });

  it('surfaces review_required as an error-tone reason', () => {
    const device = makeDevice({
      review_required: true,
      review_reason: 'Recovery probe failed 5 times',
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.label).toBe('Unhealthy');
    expect(result.reasons).toContain('Recovery probe failed 5 times');
  });

  it('falls back to a generic reason when review_required has no message', () => {
    const device = makeDevice({ review_required: true, review_reason: null });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.reasons).toContain('Operator review required');
  });

  it('combines liveness failure and lifecycle suppression in reasons', () => {
    const device = makeDevice({
      health_summary: { healthy: false, summary: 'Disconnected', last_checked_at: null },
      lifecycle_policy_summary: {
        state: 'suppressed',
        label: 'Suppressed',
        detail: 'Node restart failed',
        backoff_until: null,
      },
    });
    const result = deriveUnifiedHealth(device);
    expect(result.tone).toBe('error');
    expect(result.reasons).toEqual(['Disconnected', 'Node restart failed']);
  });
});
