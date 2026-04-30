import { describe, expect, it } from 'vitest';
import { buildUpdatePayload } from './devicePageHelpers';
import type { DeviceRead } from '../../types';

const device = {
  id: 'roku-1',
  pack_id: 'appium-roku-dlenroc',
  platform_id: 'roku_network',
  platform_label: 'Roku',
  identity_scheme: 'roku_serial',
  identity_scope: 'global',
  identity_value: 'serial-1',
  connection_target: null,
  name: 'Roku',
  os_version: '12',
  host_id: 'host-1',
  availability_status: 'available',
  needs_attention: false,
  tags: {},
  manufacturer: 'Roku',
  model: 'Ultra',
  auto_manage: true,
  device_type: 'real_device',
  connection_type: 'network',
  ip_address: '192.168.1.55',
  battery_level_percent: null,
  battery_temperature_c: null,
  charging_state: null,
  hardware_health_status: 'unknown',
  hardware_telemetry_reported_at: null,
  hardware_telemetry_state: 'unknown',
  readiness_state: 'setup_required',
  missing_setup_fields: ['roku_password'],
  verified_at: null,
  reservation: null,
  lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
  health_summary: { healthy: null, summary: 'Unknown', last_checked_at: null },
  emulator_state: null,
  created_at: '2026-04-27T00:00:00Z',
  updated_at: '2026-04-27T00:00:00Z',
} satisfies DeviceRead;

describe('buildUpdatePayload', () => {
  it('stores manifest fields in device_config instead of top-level payload keys', () => {
    const payload = buildUpdatePayload(
      { host_id: 'host-1', device_config: { roku_password: 'secret123' } },
      device,
      {},
    );
    expect(payload.device_config).toEqual({ roku_password: 'secret123' });
    expect(payload).not.toHaveProperty('roku_password');
  });
});
