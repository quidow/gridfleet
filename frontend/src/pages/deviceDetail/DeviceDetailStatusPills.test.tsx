import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import DeviceDetailStatusPills from './DeviceDetailStatusPills';
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
    availability_status: 'available',
    tags: null,
    auto_manage: true,
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
      healthy: true,
      summary: 'Healthy',
      last_checked_at: '2026-03-30T10:00:03Z',
    },
    emulator_state: null,
    created_at: '2026-03-30T10:00:03Z',
    updated_at: '2026-03-30T10:00:03Z',
    appium_node: null,
    sessions: [],
    ...overrides,
  };
}

describe('DeviceDetailStatusPills', () => {
  it('renders two status pills', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills device={makeDevice()} />
      </MemoryRouter>,
    );
    expect(screen.getAllByTestId('device-detail-status-pill')).toHaveLength(2);
  });

  it('connectivity pill links to triage tab with device-health anchor', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills device={makeDevice()} />
      </MemoryRouter>,
    );
    const links = screen.getAllByRole('link');
    const connectivityLink = links.find((el) =>
      el.getAttribute('aria-label')?.toLowerCase().includes('connectivity'),
    );
    expect(connectivityLink).toBeDefined();
    expect(connectivityLink).toHaveAttribute('href', '/devices/device-1?tab=triage#device-health');
  });

  it('connectivity pill reflects unhealthy tone when health_summary.healthy is false', () => {
    render(
      <MemoryRouter>
        <DeviceDetailStatusPills
          device={makeDevice({
            health_summary: {
              healthy: false,
              summary: 'ADB not responsive',
              last_checked_at: null,
            },
          })}
        />
      </MemoryRouter>,
    );
    const connectivityLink = screen.getAllByRole('link').find((el) =>
      el.getAttribute('aria-label')?.toLowerCase().includes('connectivity'),
    );
    expect(connectivityLink).toBeDefined();
    expect(connectivityLink).toHaveAttribute('href', '/devices/device-1?tab=triage#device-health');
  });
});
