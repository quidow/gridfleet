import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { DeviceRead } from '../../types';
import { ActivityCard } from './ActivityCard';

const mockRuns = vi.fn<() => { data: unknown; status: string }>(() => ({ data: [], status: 'success' }));
const mockDevices = vi.fn<() => { data: DeviceRead[] | undefined; status: string }>(
  () => ({ data: [], status: 'success' }),
);
vi.mock('../../hooks/useRuns', () => ({ useRuns: () => mockRuns() }));
vi.mock('../../hooks/useDevices', () => ({ useDevices: () => mockDevices() }));

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
      <ActivityCard />
    </MemoryRouter>,
  );
}

describe('ActivityCard', () => {
  it('renders the section heading', () => {
    renderCard();
    expect(screen.getByRole('heading', { name: 'Activity' })).toBeInTheDocument();
  });

  it('collapses empty sections into their header line', () => {
    renderCard();
    expect(screen.getByRole('heading', { name: /Active runs · none/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Busy outside runs · none/ })).toBeInTheDocument();
  });

  it('nests reserved devices with live state under their run', () => {
    mockRuns.mockReturnValueOnce({
      data: [{
        id: 'run-1',
        name: 'Nightly',
        state: 'active',
        reserved_devices: [
          { device_id: 'd1', name: 'Pixel 8', identity_value: 'serial-001', platform_id: 'android_mobile', platform_label: null },
        ],
        created_at: '2026-06-10T11:00:00Z',
        started_at: '2026-06-10T11:00:00Z',
      }],
      status: 'success',
    });
    mockDevices.mockReturnValueOnce({
      data: [makeDevice({ id: 'd1', name: 'Pixel 8', operational_state: 'busy', is_reserved: true })],
      status: 'success',
    });
    renderCard();
    expect(screen.getByText('Nightly').closest('a')!.getAttribute('href')).toBe('/runs/run-1');
    const deviceLinks = screen.getAllByText('Pixel 8');
    expect(deviceLinks).toHaveLength(1);
    expect(deviceLinks[0]!.closest('a')!.getAttribute('href')).toBe('/devices/d1');
    // reserved device is NOT repeated under "busy outside runs"
    expect(screen.getByRole('heading', { name: /Busy outside runs · none/ })).toBeInTheDocument();
  });

  it('lists busy unreserved devices under busy outside runs', () => {
    mockDevices.mockReturnValueOnce({
      data: [makeDevice({ id: 'd2', name: 'Apple TV', operational_state: 'busy', is_reserved: false })],
      status: 'success',
    });
    renderCard();
    expect(screen.getByRole('heading', { name: /Active runs · none/ })).toBeInTheDocument();
    expect(screen.getByText('Apple TV').closest('a')!.getAttribute('href')).toBe('/devices/d2');
  });
});
