import { describe, expect, it } from 'vitest';
import type { DeviceDetail, DeviceHealth } from '../../types';
import { deriveDeviceDetailTriage } from './deviceDetailTriage';

function makeDevice(overrides: Partial<DeviceDetail> = {}): DeviceDetail {
  return {
    id: 'device-1',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    identity_scheme: 'adb_serial',
    identity_scope: 'global' as const,
    identity_value: 'device-1',
    connection_target: 'device-1',
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
    connection_type: 'usb',
    ip_address: null,
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
    appium_node: {
      id: 'node-1',
      port: 4723,
      grid_url: 'http://hub:4444',
      pid: 100,
      container_id: null,
      active_connection_target: 'device-1',
      desired_state: 'running',
      effective_state: 'running',
      started_at: '2026-03-30T10:00:03Z',
    },
    sessions: [],
    ...overrides,
  };
}

function makeHealth(overrides: Partial<DeviceHealth> = {}): DeviceHealth {
  return {
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    healthy: true,
    node: {
      running: true,
      port: 4723,
      state: 'running',
    },
    device_checks: { healthy: true },
    session_viability: null,
    lifecycle_policy: {
      last_failure_source: null,
      last_failure_reason: null,
      last_action: null,
      last_action_at: null,
      stop_pending: false,
      stop_pending_reason: null,
      stop_pending_since: null,
      excluded_from_run: false,
      excluded_run_id: null,
      excluded_run_name: null,
      excluded_at: null,
      will_auto_rejoin_run: false,
      recovery_suppressed_reason: null,
      backoff_until: null,
      recovery_state: 'idle',
    },
    ...overrides,
  };
}

describe('deriveDeviceDetailTriage', () => {
  it('returns all-clear action for verified healthy device', () => {
    const triage = deriveDeviceDetailTriage(makeDevice(), { health: makeHealth(), canTestSession: true });

    expect(triage).toMatchObject({
      tone: 'ok',
      title: 'Device ready for sessions',
      action: { kind: 'test-session', label: 'Test Session' },
    });
  });

  it('prioritizes stopped emulator launch action', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        device_type: 'emulator',
        connection_type: 'virtual',
        operational_state: 'offline', hold: null,
        emulator_state: 'stopped',
      }),
      { canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      title: 'Emulator is not running',
      action: { kind: 'launch-emulator', label: 'Launch Emulator' },
    });
  });

  it('prioritizes stopped simulator boot action', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        platform_id: 'ios',
        device_type: 'simulator',
        connection_type: 'virtual',
        operational_state: 'offline', hold: null,
        emulator_state: 'shutdown',
      }),
      { canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      title: 'Simulator is not running',
      action: { kind: 'boot-simulator', label: 'Boot Simulator' },
    });
  });

  it('prioritizes node stopped after virtual device state', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        appium_node: {
          ...makeDevice().appium_node!,
          pid: null,
          active_connection_target: null,
          desired_state: 'stopped',
          effective_state: 'stopped',
        },
      }),
      { canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      title: 'Appium node is stopped',
      action: { kind: 'start-node', label: 'Start Node' },
    });
  });

  it('returns neutral tone when verified device has no node yet', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ appium_node: null }),
      { canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'neutral',
      eyebrow: 'Node idle',
      title: 'No Appium node configured',
      action: { kind: 'start-node', label: 'Start Node' },
    });
  });

  it('returns neutral tone with Maintenance eyebrow when device is in maintenance', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ operational_state: 'available', hold: 'maintenance', appium_node: null }),
      { canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'neutral',
      eyebrow: 'Maintenance',
      action: { kind: 'open-control' },
    });
  });

  it('prioritizes unhealthy health snapshot after running node', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ health_summary: { healthy: false, summary: 'ADB not responsive', last_checked_at: null } }),
      {
        health: makeHealth({
          healthy: false,
          device_checks: { healthy: false, detail: 'ADB not responsive' },
        }),
        canTestSession: false,
      },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      title: 'Device health check failed',
      action: { kind: 'open-control', label: 'Review Control' },
    });
  });

  it('prioritizes review_required card over every other branch', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        review_required: true,
        review_reason: 'Recovery probe failed 5 times',
        review_set_at: '2026-05-17T15:00:00Z',
        // Conditions that would otherwise drive a lower-tone card; review must win.
        operational_state: 'offline',
        hardware_health_status: 'warning',
        appium_node: null,
      }),
      { health: makeHealth(), canTestSession: false },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Review required',
      title: 'Device shelved — operator review required',
      detail: 'Recovery probe failed 5 times',
      action: { kind: 'open-control' },
    });
    expect(triage.evidence[0]).toMatchObject({ label: 'Reason', value: 'Recovery probe failed 5 times' });
  });

  it('falls back to a generic detail when review_reason is missing', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ review_required: true, review_reason: null }),
      { health: makeHealth(), canTestSession: false },
    );
    expect(triage.detail).toContain('Automated recovery hit the failure threshold');
  });

  it('keeps unsupported telemetry passive while surfacing stale telemetry', () => {
    const unsupported = deriveDeviceDetailTriage(
      makeDevice({ hardware_telemetry_state: 'unsupported' }),
      { health: makeHealth(), canTestSession: false },
    );
    const stale = deriveDeviceDetailTriage(
      makeDevice({ hardware_telemetry_state: 'stale' }),
      { health: makeHealth(), canTestSession: false },
    );

    expect(unsupported).toMatchObject({
      tone: 'ok',
      title: 'Device ready for sessions',
      action: { kind: 'open-control' },
    });
    expect(unsupported.evidence).toContainEqual({ label: 'Telemetry', value: 'No telemetry', tone: 'neutral' });
    expect(stale).toMatchObject({
      tone: 'warn',
      title: 'Hardware Stale',
      action: { kind: 'open-hardware-filter' },
    });
  });
});
