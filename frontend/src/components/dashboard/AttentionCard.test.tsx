import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { DeviceRead, LifecycleIncidentRead } from '../../types';
import { AttentionCard } from './AttentionCard';

const mockDevices = vi.fn<() => { data: DeviceRead[] | undefined; status: string }>(
  () => ({ data: [], status: 'success' }),
);
const mockIncidents = vi.fn<() => { data: LifecycleIncidentRead[] | undefined; status: string }>(
  () => ({ data: [], status: 'success' }),
);
vi.mock('../../hooks/useDevices', () => ({ useDevices: () => mockDevices() }));
vi.mock('../../hooks/useLifecycle', () => ({
  useRecentLifecycleIncidents: () => mockIncidents(),
}));

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
    tags: null,
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
    emulator_state: null,
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

function renderCard() {
  return render(
    <MemoryRouter>
      <AttentionCard />
    </MemoryRouter>,
  );
}

describe('AttentionCard', () => {
  it('renders the all-clear state when nothing needs attention', () => {
    renderCard();
    expect(screen.getByText('Nothing needs attention.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Needs attention/ })).toBeInTheDocument();
  });

  it('renders rows with device link, reason, and badge', () => {
    mockDevices.mockReturnValueOnce({
      data: [
        makeDevice({
          id: 'device-1',
          name: 'Apple TV',
          lifecycle_policy_summary: { state: 'backoff', label: 'Waiting to Retry', detail: 'Backing off', backoff_until: null },
        }),
      ],
      status: 'success',
    });
    renderCard();
    expect(screen.getByText('Apple TV').closest('a')!.getAttribute('href')).toBe('/devices/device-1');
    expect(screen.getByText('Backing off')).toBeInTheDocument();
    expect(screen.getByText('Waiting to Retry')).toBeInTheDocument();
  });

  it('caps rows at 5 with overflow link', () => {
    mockDevices.mockReturnValueOnce({
      data: Array.from({ length: 7 }, (_, i) =>
        makeDevice({ id: `device-${i}`, name: `Device ${i}`, needs_attention: true }),
      ),
      status: 'success',
    });
    renderCard();
    expect(screen.getByText('+ 2 more').closest('a')!.getAttribute('href')).toBe('/devices?needs_attention=true');
  });

  it('stays all-clear for success-tone incidents', () => {
    mockIncidents.mockReturnValueOnce({
      data: [makeIncident({ device_id: 'device-1', device_name: 'Apple TV', event_type: 'lifecycle_recovered', label: 'Recovered' })],
      status: 'success',
    });
    mockDevices.mockReturnValueOnce({ data: [makeDevice({ id: 'device-1', name: 'Apple TV' })], status: 'success' });
    renderCard();
    expect(screen.getByText('Nothing needs attention.')).toBeInTheDocument();
    expect(screen.queryByText(/Recovered/)).not.toBeInTheDocument();
  });
});
