import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import DeviceHealthCell from './DeviceHealthCell';
import type { DeviceRead } from '../../types';

function baseDevice(overrides: Partial<DeviceRead>): DeviceRead {
  return {
    ...({
      id: 'd1',
      name: 'Test',
      pack_id: 'appium-roku-dlenroc',
      platform_id: 'roku_network',
      platform_label: 'Roku Network',
      os_version: '15.1.4',
      operational_state: 'available', hold: null,
      needs_attention: false,
      readiness_state: 'verified',
      auto_manage: true,
      host_id: 'h1',
      identity_value: '192.168.1.2',
      tags: {},
      sessions: [],
      health_summary: { healthy: true, summary: '' },
      lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null },
      missing_setup_fields: [],
      hardware_telemetry_state: 'unsupported',
      hardware_health_status: 'ok',
      battery_level_percent: null,
      charging_state: null,
      device_type: 'real_device',
      connection_type: 'network',
      emulator_state: null,
      reservation: null,
    } as unknown as DeviceRead),
    ...overrides,
  };
}

describe('DeviceHealthCell', () => {
  it('renders inline label without min-height spacer when healthy', () => {
    const { container } = render(<DeviceHealthCell device={baseDevice({})} />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).not.toContain('min-h-12');
  });

  it('still renders the label for ok tone', () => {
    render(<DeviceHealthCell device={baseDevice({})} />);
    expect(screen.getByText(/Healthy|Available|Verified/i)).toBeInTheDocument();
  });
});
