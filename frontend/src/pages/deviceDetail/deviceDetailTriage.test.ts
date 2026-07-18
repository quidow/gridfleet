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
    operational_state: 'available',
    tags: null,
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
      device: { status: 'ok', detail: null, checked_at: '2026-03-30T10:00:03Z' },
      node: { status: 'ok', detail: 'running', checked_at: null },
      viability: { status: 'unknown', detail: 'not run', checked_at: null },
      overall: 'ok',
    },
    emulator_state: null,
    created_at: '2026-03-30T10:00:03Z',
    updated_at: '2026-03-30T10:00:03Z',
    appium_node: {
      id: 'node-1',
      port: 4723,
      pid: 100,
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
      deferred_stop: false,
      deferred_stop_reason: null,
      deferred_stop_since: null,
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
  it('returns ready card for healthy available device', () => {
    const triage = deriveDeviceDetailTriage(makeDevice(), { health: makeHealth() });

    expect(triage).toMatchObject({
      tone: 'ok',
      eyebrow: 'Ready',
      title: 'Device ready for sessions',
      action: { kind: 'none' },
    });
  });

  it('shows start-node for stopped node', () => {
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
      {},
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
      {},
    );

    expect(triage).toMatchObject({
      tone: 'neutral',
      eyebrow: 'Node idle',
      title: 'No Appium node configured',
      action: { kind: 'start-node', label: 'Start Node' },
    });
  });

  it('shows maintenance with exit action and reason', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        operational_state: 'maintenance',
        appium_node: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
          maintenance_reason: 'Cooldown escalation',
        },
      }),
      {},
    );

    expect(triage).toMatchObject({
      tone: 'neutral',
      eyebrow: 'Maintenance',
      title: 'In maintenance',
      detail: 'Cooldown escalation',
      action: { kind: 'exit-maintenance', label: 'Take out of maintenance' },
    });
  });

  it('shows connectivity lost when health checks fail with node stopped', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        operational_state: 'offline',
        health_summary: {
          device: { status: 'failed', detail: 'Disconnected', checked_at: null },
          node: { status: 'failed', detail: 'error', checked_at: null },
          viability: { status: 'unknown', detail: 'not run', checked_at: null },
          overall: 'failed',
        },
        appium_node: {
          ...makeDevice().appium_node!,
          effective_state: 'error',
        },
      }),
      {},
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Connectivity',
      title: 'Device connectivity lost',
    });
  });

  it('shows reserved card with run link', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        is_reserved: true,
        reservation: {
          run_id: 'run-1',
          run_name: 'my-test-run',
          excluded: false,
        } as DeviceDetail['reservation'],
      }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'info',
      eyebrow: 'Reserved',
      title: 'Reserved by',
      titleLink: { text: 'my-test-run', to: '/runs/run-1' },
    });
  });

  it('shows busy+reserved with run context', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        operational_state: 'busy',
        is_reserved: true,
        reservation: {
          run_id: 'run-1',
          run_name: 'my-test-run',
          excluded: false,
        } as DeviceDetail['reservation'],
      }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      eyebrow: 'Busy',
      title: 'Running a session — reserved by',
      titleLink: { text: 'my-test-run' },
    });
  });

  it('shows draining state for busy+maintenance', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        operational_state: 'busy',
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
          maintenance_reason: 'Operator entered maintenance',
        },
      }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      eyebrow: 'Draining',
      title: 'Session active — maintenance pending',
      detail: 'Operator entered maintenance',
    });
  });

  it('shows verifying state', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ operational_state: 'verifying' }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      eyebrow: 'Verifying',
      title: 'Verification in progress',
    });
  });

  it('shows busy state', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ operational_state: 'busy' }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      eyebrow: 'Busy',
      title: 'Running a session',
    });
  });

  it('prioritizes review_required over everything', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        review_required: true,
        review_reason: 'Recovery probe failed 5 times',
        review_set_at: '2026-05-17T15:00:00Z',
        operational_state: 'offline',
        hardware_health_status: 'warning',
        appium_node: null,
      }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Review required',
      title: 'Device shelved — operator review required',
      detail: 'Recovery probe failed 5 times',
      action: { kind: 'none' },
    });
  });

  it('keeps unsupported telemetry passive while surfacing stale telemetry', () => {
    const unsupported = deriveDeviceDetailTriage(
      makeDevice({ hardware_telemetry_state: 'unsupported' }),
      { health: makeHealth() },
    );
    const stale = deriveDeviceDetailTriage(
      makeDevice({ hardware_telemetry_state: 'stale' }),
      { health: makeHealth() },
    );

    expect(unsupported).toMatchObject({
      tone: 'ok',
      title: 'Device ready for sessions',
      action: { kind: 'none' },
    });
    expect(stale).toMatchObject({
      tone: 'warn',
      title: 'Hardware Stale',
      action: { kind: 'open-hardware-filter' },
    });
  });

  it('shows health check failed when node is running but health is unhealthy', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        health_summary: {
          device: { status: 'failed', detail: 'ADB not responsive', checked_at: null },
          node: { status: 'ok', detail: 'running', checked_at: null },
          viability: { status: 'unknown', detail: 'not run', checked_at: null },
          overall: 'failed',
        },
      }),
      {
        health: makeHealth({
          healthy: false,
          device_checks: { healthy: false, detail: 'ADB not responsive' },
        }),
      },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Health check',
      title: 'Device health check failed',
    });
  });

  it('shows hardware warning when hardware health status is warning', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ hardware_health_status: 'warning' }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'warn',
      eyebrow: 'Hardware telemetry',
      action: { kind: 'open-hardware-filter' },
    });
  });

  it('shows hardware critical when hardware health status is critical', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({ hardware_health_status: 'critical' }),
      { health: makeHealth() },
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Hardware telemetry',
      action: { kind: 'open-hardware-filter' },
    });
  });

  it('shows connectivity lost with reservation context', () => {
    const triage = deriveDeviceDetailTriage(
      makeDevice({
        operational_state: 'offline',
        is_reserved: true,
        health_summary: {
          device: { status: 'failed', detail: 'Disconnected', checked_at: null },
          node: { status: 'failed', detail: 'error', checked_at: null },
          viability: { status: 'unknown', detail: 'not run', checked_at: null },
          overall: 'failed',
        },
        appium_node: { ...makeDevice().appium_node!, effective_state: 'error' },
        reservation: {
          run_id: 'run-1',
          run_name: 'my-run',
          excluded: false,
        } as DeviceDetail['reservation'],
      }),
      {},
    );

    expect(triage).toMatchObject({
      tone: 'error',
      eyebrow: 'Connectivity',
      title: 'Device connectivity lost — reserved by',
      titleLink: { text: 'my-run', to: '/runs/run-1' },
    });
  });
});
