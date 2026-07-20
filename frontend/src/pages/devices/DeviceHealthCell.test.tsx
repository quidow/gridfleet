import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { DeviceHealthCell } from './DeviceHealthCell';
import type { DeviceRead } from '../../types';

const healthSummary = (over: Partial<DeviceRead['health_summary']> = {}): DeviceRead['health_summary'] => ({
  device: { status: 'ok', detail: null, checked_at: null },
  node: { status: 'ok', detail: 'running', checked_at: null },
  viability: { status: 'unknown', detail: 'not run', checked_at: null },
  overall: 'ok',
  ...over,
});

function baseDevice(overrides: Partial<DeviceRead>): DeviceRead {
  return {
    ...({
      id: 'd1',
      name: 'Test',
      pack_id: 'appium-roku-dlenroc',
      platform_id: 'roku_network',
      platform_label: 'Roku Network',
      os_version: '15.1.4',
      operational_state: 'available',
      needs_attention: false,
      readiness_state: 'verified',
      host_id: 'h1',
      identity_value: '192.168.1.2',
      sessions: [],
      health_summary: healthSummary(),
      lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null },
      missing_setup_fields: [],
      device_type: 'real_device',
      connection_type: 'network',
      reservation: null,
    } as unknown as DeviceRead),
    ...overrides,
  };
}

describe('DeviceHealthCell', () => {
  it('renders three dots with per-signal tones', () => {
    render(
      <DeviceHealthCell
        device={baseDevice({
          health_summary: healthSummary({ node: { status: 'failed', detail: 'error', checked_at: null } }),
        })}
      />,
    );
    expect(screen.getByLabelText(/device ok/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/node failed/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/viability unknown/i)).toBeInTheDocument();
  });

  it('opens popover with the three-row breakdown', async () => {
    render(
      <DeviceHealthCell
        device={baseDevice({
          health_summary: healthSummary({
            device: { status: 'failed', detail: 'adb unreachable', checked_at: '2026-06-07T12:00:00Z' },
          }),
        })}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /health details for test/i }));

    expect(screen.getByText('Device')).toBeInTheDocument();
    expect(screen.getByText('Node')).toBeInTheDocument();
    expect(screen.getByText('Viability')).toBeInTheDocument();
    expect(screen.getByText(/adb unreachable/)).toBeInTheDocument();
    expect(screen.getByText(/checked/i)).toBeInTheDocument();
  });

});
