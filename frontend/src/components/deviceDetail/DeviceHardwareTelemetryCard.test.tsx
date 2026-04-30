import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import DeviceHardwareTelemetryCard from './DeviceHardwareTelemetryCard';
import type { DeviceDetail } from '../../types';

const baseDevice: Partial<DeviceDetail> = {
  hardware_health_status: 'unknown',
  hardware_telemetry_state: 'unknown',
  hardware_telemetry_reported_at: null,
  battery_level_percent: null,
  charging_state: null,
  battery_temperature_c: null,
};

describe('DeviceHardwareTelemetryCard', () => {
  it('collapses to single "Not reported" message when every field is null', () => {
    render(<DeviceHardwareTelemetryCard device={baseDevice as DeviceDetail} />);
    expect(screen.getByText(/Not reported/i)).toBeInTheDocument();
    expect(screen.queryByText('Battery Level')).toBeNull();
  });

  it('renders rows when at least one field has data', () => {
    render(
      <DeviceHardwareTelemetryCard
        device={{ ...baseDevice, battery_level_percent: 82, hardware_telemetry_reported_at: '2026-04-17T00:00:00Z' } as DeviceDetail}
      />,
    );
    expect(screen.getByText('Battery Level')).toBeInTheDocument();
    expect(screen.queryByText(/Not reported/i)).toBeNull();
  });

  it('does not render battery rows for unsupported telemetry with only a timestamp', () => {
    render(
      <DeviceHardwareTelemetryCard
        device={
          {
            ...baseDevice,
            hardware_telemetry_state: 'unsupported',
            hardware_telemetry_reported_at: '2026-04-17T00:00:00Z',
          } as DeviceDetail
        }
      />,
    );

    expect(screen.getByText(/not supported/i)).toBeInTheDocument();
    expect(screen.queryByText('Battery Level')).toBeNull();
  });

  it('renders temperature-only telemetry without empty battery rows', () => {
    render(
      <DeviceHardwareTelemetryCard
        device={
          {
            ...baseDevice,
            hardware_telemetry_state: 'fresh',
            battery_temperature_c: 25,
            hardware_telemetry_reported_at: '2026-04-17T00:00:00Z',
          } as DeviceDetail
        }
      />,
    );

    expect(screen.getByText('Temperature')).toBeInTheDocument();
    expect(screen.getByText('25.0C')).toBeInTheDocument();
    expect(screen.queryByText('Battery Level')).toBeNull();
    expect(screen.queryByText('Charging State')).toBeNull();
  });
});
