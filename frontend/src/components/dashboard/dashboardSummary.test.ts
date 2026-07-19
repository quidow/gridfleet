import { describe, expect, it } from 'vitest';
import type { DeviceRead, LifecycleIncidentRead } from '../../types';
import {
  deriveAttentionRows,
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
    operational_state: 'available',
    needs_attention: false,
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
    label: 'Waiting to Retry',
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

  it('counts needsAttention, maintenance, and availability in fleet summary', () => {
    const summary = deriveDashboardFleetSummary([
      makeDevice({ id: 'backoff1', needs_attention: true, operational_state: 'available', lifecycle_policy_summary: { state: 'backoff', label: 'Waiting to Retry', detail: null, backoff_until: null } }),
      makeDevice({ id: 'backoff2', needs_attention: true, operational_state: 'available', lifecycle_policy_summary: { state: 'backoff', label: 'Waiting to Retry', detail: null, backoff_until: null } }),
      makeDevice({ id: 'excluded1', needs_attention: true, operational_state: 'available', lifecycle_policy_summary: { state: 'excluded', label: 'Excluded from Run', detail: null, backoff_until: null } }),
      makeDevice({ id: 'maintenance', needs_attention: false, operational_state: 'maintenance' }),
      makeDevice({ id: 'reserved', needs_attention: false, operational_state: 'available', is_reserved: true }),
      makeDevice({ id: 'verifying', needs_attention: false, operational_state: 'verifying' }),
      makeDevice({ id: 'ok1', needs_attention: false }),
    ]);
    expect(summary.needsAttention).toBe(3);
    expect(summary.maintenance).toBe(1);
    expect(summary.busy).toBe(1);
    // Reservation is orthogonal: the reserved-but-idle device counts as available.
    expect(summary.available).toBe(5);
  });

  it('maps grid status into dashboard health states', () => {
    expect(getGridHealth(undefined)).toBeNull();
    expect(
      getGridHealth({
        ready: true,
        message: 'gridfleet control plane',
        registry: { device_count: 0, devices: [] },
        active_sessions: 0,
        active_session_ids: [],
        running_node_count: 0,
        queue_size: 0,
        queued_request_ids: [],
      }),
    ).toMatchObject({ tone: 'ready', label: 'Ready' });
    expect(
      getGridHealth({
        ready: false,
        message: 'gridfleet control plane',
        registry: { device_count: 0, devices: [] },
        active_sessions: 0,
        active_session_ids: [],
        running_node_count: 0,
        queue_size: 0,
        queued_request_ids: [],
      }),
    ).toMatchObject({ tone: 'warning', label: 'Idle' });
  });

  it('maps incident event_type to badge tone', () => {
    expect(incidentToneFromEventType('lifecycle_run_excluded')).toBe('critical');
    expect(incidentToneFromEventType('node_crash')).toBe('critical');
    expect(incidentToneFromEventType('lifecycle_recovery_failed')).toBe('critical');
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

describe('deriveAttentionRows', () => {
  it('dedupes a device that is both in recovery and has an incident into one row, reason from incident', () => {
    const device = makeDevice({
      id: 'device-1',
      name: 'Apple TV',
      needs_attention: true,
      lifecycle_policy_summary: { state: 'backoff', label: 'Waiting to Retry', detail: 'Policy detail', backoff_until: null },
    });
    const incident = makeIncident({
      device_id: 'device-1',
      device_name: 'Apple TV',
      event_type: 'lifecycle_recovery_backoff',
      detail: 'Automatic recovery is backing off',
      created_at: '2026-06-10T12:00:00Z',
    });

    const result = deriveAttentionRows([device], [incident]);

    expect(result.total).toBe(1);
    expect(result.rows).toHaveLength(1);
    expect(result.rows[0]).toMatchObject({
      deviceId: 'device-1',
      deviceName: 'Apple TV',
      reason: 'Automatic recovery is backing off',
      tone: 'warning',
      latestAt: '2026-06-10T12:00:00Z',
    });
    expect(result.rows[0]!.lifecycleSummary).not.toBeNull();
  });

  it('includes needs_attention devices without incidents, falling back to lifecycle detail then maintenance_reason', () => {
    const withDetail = makeDevice({
      id: 'device-detail',
      needs_attention: true,
      lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: 'Operator note', backoff_until: null },
    });
    const withMaintenance = makeDevice({
      id: 'device-maint',
      needs_attention: true,
      lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, maintenance_reason: 'Battery swollen', backoff_until: null },
    });

    const result = deriveAttentionRows([withDetail, withMaintenance], []);

    expect(result.total).toBe(2);
    const reasons = new Map(result.rows.map((row) => [row.deviceId, row.reason]));
    expect(reasons.get('device-detail')).toBe('Operator note');
    expect(reasons.get('device-maint')).toBe('Battery swollen');
    // idle summary → no lifecycle badge; generic fallback label
    expect(result.rows[0]!.lifecycleSummary).toBeNull();
    expect(result.rows[0]!.badgeLabel).toBe('Needs attention');
  });

  it('ignores success-tone incidents — they never produce rows', () => {
    const incident = makeIncident({
      device_id: 'device-1',
      device_name: 'Apple TV',
      event_type: 'lifecycle_recovered',
      label: 'Recovered',
      created_at: '2026-06-10T12:00:00Z',
    });

    const result = deriveAttentionRows([makeDevice({ id: 'device-1', name: 'Apple TV' })], [incident]);

    expect(result.total).toBe(0);
    expect(result.rows).toEqual([]);
  });

  it('does not create a row from an incident or active lifecycle summary alone — membership is the needs_attention flag', () => {
    const incidentOnly = makeDevice({ id: 'device-1', name: 'Fire TV' });
    const lifecycleOnly = makeDevice({
      id: 'device-2',
      lifecycle_policy_summary: { state: 'backoff', label: 'Waiting to Retry', detail: null, backoff_until: null },
    });
    const incident = makeIncident({
      device_id: 'device-1',
      device_name: 'Fire TV',
      event_type: 'node_crash',
      label: 'Node Crash',
      created_at: '2026-06-10T12:00:00Z',
    });

    const result = deriveAttentionRows([incidentOnly, lifecycleOnly], [incident]);

    expect(result.total).toBe(0);
    expect(result.rows).toEqual([]);
  });

  it('enriches a flagged device row with its unresolved incident tone and label', () => {
    const device = makeDevice({ id: 'device-1', name: 'Fire TV', needs_attention: true });
    const incident = makeIncident({
      device_id: 'device-1',
      device_name: 'Fire TV',
      event_type: 'node_crash',
      label: 'Node Crash',
      created_at: '2026-06-10T12:00:00Z',
    });

    const result = deriveAttentionRows([device], [incident]);

    expect(result.total).toBe(1);
    expect(result.rows[0]!.tone).toBe('critical');
    expect(result.rows[0]!.badgeLabel).toBe('Node Crash');
  });

  it('sorts critical rows before warning rows, then newest first', () => {
    const devices = [
      makeDevice({ id: 'warn-old', needs_attention: true }),
      makeDevice({ id: 'crit', needs_attention: true }),
      makeDevice({ id: 'warn-new', needs_attention: true }),
    ];
    const incidents = [
      makeIncident({ device_id: 'warn-old', event_type: 'health_check_fail', created_at: '2026-06-10T10:00:00Z' }),
      makeIncident({ device_id: 'crit', event_type: 'node_crash', created_at: '2026-06-10T09:00:00Z' }),
      makeIncident({ device_id: 'warn-new', event_type: 'health_check_fail', created_at: '2026-06-10T11:00:00Z' }),
    ];

    const result = deriveAttentionRows(devices, incidents);

    expect(result.rows.map((row) => row.deviceId)).toEqual(['crit', 'warn-new', 'warn-old']);
  });
});
