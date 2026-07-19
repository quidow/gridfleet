import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { DeviceDetailStatusPills } from './DeviceDetailStatusPills';
import type { DeviceDetail } from '../../types';

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
    health_summary: {
      device: { status: 'ok', detail: null, checked_at: '2026-03-30T10:00:03Z' },
      node: { status: 'ok', detail: 'running', checked_at: null },
      viability: { status: 'unknown', detail: 'not run', checked_at: null },
      overall: 'ok',
    },
    created_at: '2026-03-30T10:00:03Z',
    updated_at: '2026-03-30T10:00:03Z',
    appium_node: null,
    sessions: [],
    ...overrides,
  };
}

describe('DeviceDetailStatusPills', () => {
  it('renders four status pills', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills device={makeDevice()} />
      </MemoryRouter>,
    );
    expect(screen.getAllByLabelText(/^(Hardware|Device|Node|Viability) /)).toHaveLength(4);
  });

  it('verdict pills link to triage tab with device-health anchor', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills device={makeDevice()} />
      </MemoryRouter>,
    );
    const links = screen.getAllByRole('link');
    const deviceLink = links.find((el) =>
      el.getAttribute('aria-label')?.toLowerCase().startsWith('device'),
    );
    expect(deviceLink).toBeDefined();
    expect(deviceLink).toHaveAttribute('href', '/devices/device-1?tab=triage#device-health');
  });

  it('device pill surfaces the failed detail when the device verdict fails', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills
          device={makeDevice({
            health_summary: {
              device: { status: 'failed', detail: 'ADB not responsive', checked_at: null },
              node: { status: 'ok', detail: 'running', checked_at: null },
              viability: { status: 'unknown', detail: 'not run', checked_at: null },
              overall: 'failed',
            },
          })}
        />
      </MemoryRouter>,
    );
    const deviceLink = screen.getAllByRole('link').find((el) =>
      el.getAttribute('aria-label')?.toLowerCase().startsWith('device'),
    );
    expect(deviceLink).toBeDefined();
    expect(deviceLink).toHaveAttribute('aria-label', 'Device ADB not responsive');
  });
});
