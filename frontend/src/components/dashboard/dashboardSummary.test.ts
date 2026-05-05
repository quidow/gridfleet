import { describe, expect, it } from 'vitest';
import type { DeviceRead, LifecycleIncidentRead } from '../../types';
import {
  deriveDashboardFleetSummary,
  getGridHealth,
  groupLifecycleIncidents,
  incidentToneFromEventType,
} from './dashboardSummary';

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
    os_version: '14',
    host_id: 'host-1',
    operational_state: 'available', hold: null,
    needs_attention: false,
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

function makeIncident(overrides: Partial<LifecycleIncidentRead> = {}): LifecycleIncidentRead {
  return {
    id: `incident-${Math.random().toString(36).slice(2)}`,
    device_id: 'device-1',
    device_name: 'Pixel 8',
    device_identity_value: 'serial-001',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    event_type: 'lifecycle_recovery_backoff',
    label: 'Recovery Backoff',
    summary_state: 'backoff',
    reason: 'Recovery probe failed',
    detail: 'Automatic recovery is backing off before the next retry',
    source: 'session_viability',
    run_id: null,
    run_name: null,
    backoff_until: null,
    created_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

describe('dashboardSummary', () => {
  it('counts stale telemetry and hardware warnings independently', () => {
    const summary = deriveDashboardFleetSummary([
      makeDevice({ id: 'unsupported', hardware_telemetry_state: 'unsupported' }),
      makeDevice({ id: 'stale', hardware_telemetry_state: 'stale' }),
      makeDevice({ id: 'warning', hardware_health_status: 'warning' }),
    ]);

    expect(summary.staleTelemetry).toBe(1);
    expect(summary.hardwareWarning).toBe(1);
  });

  it('counts hardware critical devices', () => {
    const summary = deriveDashboardFleetSummary([
      makeDevice({ id: 'critical', hardware_health_status: 'critical' }),
      makeDevice({ id: 'stale', hardware_telemetry_state: 'stale' }),
    ]);

    expect(summary.hardwareCritical).toBe(1);
    expect(summary.staleTelemetry).toBe(1);
  });

  it('groups repeated lifecycle incidents by device, state, and label', () => {
    const grouped = groupLifecycleIncidents([
      makeIncident({ id: 'older', created_at: '2026-04-16T12:00:00Z', reason: 'Old reason' }),
      makeIncident({ id: 'newer', created_at: '2026-04-16T12:03:00Z', reason: 'Latest reason' }),
      makeIncident({
        id: 'other-state',
        summary_state: 'manual',
        label: 'Manual Recovery',
        created_at: '2026-04-16T12:02:00Z',
      }),
    ]);

    expect(grouped).toHaveLength(2);
    expect(grouped[0]).toMatchObject({
      count: 2,
      latestCreatedAt: '2026-04-16T12:03:00Z',
      reason: 'Latest reason',
    });
    expect(grouped[1]).toMatchObject({ count: 1, summaryState: 'manual' });
  });

  it('counts needsAttention, maintenance, and reserved devices in fleet summary', () => {
    const summary = deriveDashboardFleetSummary([
      makeDevice({ id: 'backoff1', needs_attention: true, operational_state: 'available', hold: null, lifecycle_policy_summary: { state: 'backoff', label: 'Backing Off', detail: null, backoff_until: null } }),
      makeDevice({ id: 'backoff2', needs_attention: true, operational_state: 'available', hold: null, lifecycle_policy_summary: { state: 'backoff', label: 'Backing Off', detail: null, backoff_until: null } }),
      makeDevice({ id: 'excluded1', needs_attention: true, operational_state: 'available', hold: null, lifecycle_policy_summary: { state: 'excluded', label: 'Excluded', detail: null, backoff_until: null } }),
      makeDevice({ id: 'maintenance', needs_attention: false, operational_state: 'available', hold: 'maintenance' }),
      makeDevice({ id: 'reserved', needs_attention: false, operational_state: 'available', hold: 'reserved' }),
      makeDevice({ id: 'ok1', needs_attention: false }),
    ]);
    expect(summary.needsAttention).toBe(3);
    expect(summary.maintenance).toBe(1);
    expect(summary.reserved).toBe(1);
    expect(summary.available).toBe(4);
  });

  it('maps grid status into dashboard health states', () => {
    expect(getGridHealth(undefined)).toBeNull();
    expect(
      getGridHealth({
        grid: { ready: true },
        registry: { device_count: 0, devices: [] },
        active_sessions: 0,
        queue_size: 0,
      }),
    ).toMatchObject({ tone: 'ready', label: 'Ready' });
    expect(
      getGridHealth({
        grid: { ready: false, error: 'Grid down' },
        registry: { device_count: 0, devices: [] },
        active_sessions: 0,
        queue_size: 0,
      }),
    ).toMatchObject({ tone: 'error', label: 'Unavailable' });
  });

  it('maps incident event_type to badge tone', () => {
    expect(incidentToneFromEventType('lifecycle_run_excluded')).toBe('danger');
    expect(incidentToneFromEventType('node_crash')).toBe('danger');
    expect(incidentToneFromEventType('lifecycle_recovery_failed')).toBe('danger');
    expect(incidentToneFromEventType('lifecycle_recovery_backoff')).toBe('warning');
    expect(incidentToneFromEventType('lifecycle_deferred_stop')).toBe('warning');
    expect(incidentToneFromEventType('health_check_fail')).toBe('warning');
    expect(incidentToneFromEventType('connectivity_lost')).toBe('warning');
    expect(incidentToneFromEventType('lifecycle_recovered')).toBe('success');
    expect(incidentToneFromEventType('lifecycle_run_restored')).toBe('success');
    expect(incidentToneFromEventType('connectivity_restored')).toBe('success');
    expect(incidentToneFromEventType('node_restart')).toBe('info');
  });

  it('does not expose attention aggregate fields', () => {
    const summary = deriveDashboardFleetSummary([]) as Record<string, unknown>;
    expect(summary).not.toHaveProperty('actionDeviceCount');
    expect(summary).not.toHaveProperty('actionTone');
  });
});
