import { describe, expect, it } from 'vitest';
import type { DeviceRead, HostRead } from '../../types';
import {
  buildHostsSummaryHref,
  deriveHostsFleetStats,
  filterHostsBySummary,
  hasActiveHostsSummaryFilters,
  readHostsSummaryFilters,
} from './hostsSummary';

function makeHost(overrides: Partial<HostRead> = {}): HostRead {
  return {
    id: `host-${Math.random().toString(36).slice(2)}`,
    hostname: 'lab-host',
    ip: '10.0.0.10',
    os_type: 'linux',
    agent_port: 5100,
    status: 'online',
    agent_version: '1.0.0',
    required_agent_version: '1.0.0',
    recommended_agent_version: '1.0.0',
    agent_update_available: false,
    agent_version_status: 'ok',
    capabilities: null,
    missing_prerequisites: [],
    last_heartbeat: '2026-04-16T12:00:00Z',
    created_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

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
    name: 'Pixel 9',
    manufacturer: null,
    model: null,
    needs_attention: false,
    os_version: '15',
    host_id: 'host-1',
    operational_state: 'available',
    hold: null,
    tags: null,
    auto_manage: true,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
    battery_level_percent: 90,
    battery_temperature_c: 35,
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

describe('hostsSummary', () => {
  it('derives fleet stats with stale and unknown agents separated', () => {
    const hosts = [
      makeHost({ id: 'host-1', status: 'online', agent_version_status: 'ok' }),
      makeHost({ id: 'host-2', status: 'offline', agent_version_status: 'outdated' }),
      makeHost({ id: 'host-3', status: 'offline', agent_version_status: 'unknown' }),
    ];
    const devices = [
      makeDevice({ id: 'device-1', host_id: 'host-1' }),
      makeDevice({ id: 'device-2', host_id: 'host-2' }),
      makeDevice({ id: 'device-3', host_id: 'host-3' }),
    ];

    expect(deriveHostsFleetStats(hosts, devices)).toEqual({
      total: 3,
      online: 1,
      offline: 2,
      staleAgents: 1,
      unknownAgents: 1,
      totalMappedDevices: 3,
      offlineMappedDevices: 2,
    });
  });

  it('preserves unrelated query params while replacing summary params', () => {
    const href = buildHostsSummaryHref(
      new URLSearchParams('search=lab&status=offline&agent_version_status=outdated'),
      { status: 'online' },
    );
    const params = new URL(href, 'https://example.test').searchParams;

    expect(params.get('search')).toBe('lab');
    expect(params.get('status')).toBe('online');
    expect(params.has('agent_version_status')).toBe(false);
  });

  it('filters hosts by summary params', () => {
    const hosts = [
      makeHost({ id: 'host-1', hostname: 'alpha', status: 'online', agent_version_status: 'ok' }),
      makeHost({ id: 'host-2', hostname: 'beta', status: 'offline', agent_version_status: 'outdated' }),
      makeHost({ id: 'host-3', hostname: 'gamma', status: 'online', agent_version_status: 'outdated' }),
    ];

    const statusAndVersionFilters = readHostsSummaryFilters(
      new URLSearchParams('status=online&agent_version_status=outdated'),
    );
    expect(hasActiveHostsSummaryFilters(statusAndVersionFilters)).toBe(true);
    expect(filterHostsBySummary(hosts, statusAndVersionFilters).map((host) => host.id)).toEqual(['host-3']);
  });
});
