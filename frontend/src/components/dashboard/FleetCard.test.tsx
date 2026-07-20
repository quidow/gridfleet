import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { DeviceRead } from '../../types';
import { FleetCard } from './FleetCard';

const mockDevices = vi.fn<() => { data: DeviceRead[] | undefined }>(() => ({ data: [] }));
const mockCatalog = vi.fn(() => ({ data: [{ id: 'pack-1', state: 'enabled', platforms: [{ id: 'roku', display_name: 'Roku' }] }] }));
const mockTimeline = vi.fn<() => { data: { series: unknown[] } }>(() => ({ data: { series: [] } }));

vi.mock('../../hooks/useDevices', () => ({ useDevices: () => mockDevices() }));
vi.mock('../../hooks/useDriverPacks', () => ({ useDriverPackCatalog: () => mockCatalog() }));
vi.mock('../../hooks/useAnalytics', () => ({ useFleetCapacityTimeline: () => mockTimeline() }));

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
    is_reserved: false,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
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
    created_at: '2026-04-16T12:00:00Z',
    updated_at: '2026-04-16T12:00:00Z',
    ...overrides,
  };
}

function renderCard() {
  return render(
    <MemoryRouter>
      <FleetCard />
    </MemoryRouter>,
  );
}

describe('FleetCard', () => {
  it('renders device count, operational-state legend, and platform chips', () => {
    mockDevices.mockReturnValue({
      data: [
        makeDevice({ id: 'd1', platform_id: 'roku', is_reserved: true }),
        makeDevice({ id: 'd2', platform_id: 'roku', operational_state: 'offline' }),
      ],
    });
    renderCard();
    expect(screen.getByText('2 devices')).toBeInTheDocument();
    expect(screen.getByText('Available')).toBeInTheDocument();
    // Reservation is not a fleet state: the reserved-but-idle device renders
    // inside the Available slice and there is no Reserved legend entry.
    expect(screen.queryByText('Reserved')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Available: 1')).toBeInTheDocument();
    expect(screen.getByLabelText('Offline: 1')).toBeInTheDocument();
    expect(screen.getByLabelText(/Roku — 2 devices/)).toBeInTheDocument();
    mockDevices.mockReset();
    mockDevices.mockReturnValue({ data: [] });
  });

  it('renders the 24h fleet health chart with an analytics link', () => {
    mockDevices.mockReturnValueOnce({ data: [makeDevice({ id: 'd1' })] });
    mockTimeline.mockReturnValueOnce({
      data: {
        series: [
          { bucket_start: 'x', has_data: true, devices_total: 10, devices_offline: 5, devices_maintenance: 0 },
          { bucket_start: 'y', has_data: true, devices_total: 10, devices_offline: 1, devices_maintenance: 0 },
        ],
      },
    });
    renderCard();
    expect(screen.getByText('Fleet health')).toBeInTheDocument();
    expect(screen.getByText('Last 24 hours')).toBeInTheDocument();
    expect(screen.getByText('View in Analytics').closest('a')!.getAttribute('href')).toBe(
      '/analytics?tab=fleet-capacity',
    );
  });

  it('shows the empty state with no devices', () => {
    renderCard();
    expect(screen.getByText('No devices registered.')).toBeInTheDocument();
  });
});
