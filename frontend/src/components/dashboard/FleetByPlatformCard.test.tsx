import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import FleetByPlatformCard from './FleetByPlatformCard';
import type { DeviceRead } from '../../types';

function device(overrides: Partial<DeviceRead>): DeviceRead {
  return {
    id: overrides.id ?? 'd',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    identity_scheme: 'adb_serial',
    identity_scope: 'global' as const,
    identity_value: 'id',
    connection_target: 'id',
    name: 'X',
    manufacturer: null,
    model: null,
    os_version: '14',
    host_id: 'h',
    operational_state: 'available', hold: null,
    needs_attention: false,
    tags: null,
    auto_manage: true,
    device_type: 'real_device',
    connection_type: 'usb',
    ip_address: null,
    battery_level_percent: null,
    battery_temperature_c: null,
    charging_state: null,
    hardware_health_status: 'healthy',
    hardware_telemetry_reported_at: null,
    hardware_telemetry_state: 'fresh',
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: '2026-04-16T12:00:00Z',
    reservation: null,
    lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
    health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-04-16T12:00:00Z' },
    emulator_state: null,
    created_at: '2026-04-16T12:00:00Z',
    updated_at: '2026-04-16T12:00:00Z',
    ...overrides,
  } as DeviceRead;
}

const mockUseDevices = vi.fn();
const mockUseDriverPackCatalog = vi.fn();
let mockFleetTimeline = [
  {
    bucket_start: 't0',
    devices_total: 5,
    devices_available: 1,
    devices_offline: 1,
    devices_maintenance: 1,
    hosts_total: 1,
    hosts_online: 1,
    active_sessions: 0,
    queued_requests: 0,
  },
  {
    bucket_start: 't1',
    devices_total: 5,
    devices_available: 1,
    devices_offline: 1,
    devices_maintenance: 1,
    hosts_total: 1,
    hosts_online: 1,
    active_sessions: 0,
    queued_requests: 0,
  },
];

vi.mock('../../hooks/useDevices', () => ({
  useDevices: () => mockUseDevices(),
}));

vi.mock('../../hooks/useRetriableQueryState', () => ({
  deriveRetriableQueryState: () => 'success',
}));

vi.mock('../../hooks/useDriverPacks', () => ({
  useDriverPackCatalog: () => mockUseDriverPackCatalog(),
}));

vi.mock('../../hooks/useAnalytics', () => ({
  useFleetCapacityTimeline: () => ({
    data: {
      bucket_minutes: 15,
      series: mockFleetTimeline,
    },
  }),
}));

function makeQueryResult(devices: DeviceRead[]) {
  return { data: devices, status: 'success', refetch: vi.fn() };
}

const defaultDevices = () => [
  device({ id: 'a', operational_state: 'available', hold: null }),
  device({ id: 'b', operational_state: 'busy', hold: null }),
  device({ id: 'r', operational_state: 'available', hold: 'reserved' }),
  device({ id: 'm', operational_state: 'available', hold: 'maintenance' }),
  device({ id: 'o', operational_state: 'offline', hold: null }),
];

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FleetByPlatformCard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockFleetTimeline = [
    {
      bucket_start: 't0',
      devices_total: 5,
      devices_available: 1,
      devices_offline: 1,
      devices_maintenance: 1,
      hosts_total: 1,
      hosts_online: 1,
      active_sessions: 0,
      queued_requests: 0,
      has_data: true,
    },
    {
      bucket_start: 't1',
      devices_total: 5,
      devices_available: 1,
      devices_offline: 1,
      devices_maintenance: 1,
      hosts_total: 1,
      hosts_online: 1,
      active_sessions: 0,
      queued_requests: 0,
      has_data: true,
    },
  ];
  mockUseDevices.mockReturnValue(makeQueryResult(defaultDevices()));
  mockUseDriverPackCatalog.mockReturnValue({
    data: [{ id: 'appium-uiautomator2', state: 'enabled', platforms: [] }],
  });
});

describe('FleetByPlatformCard palette', () => {
  it('renders 5 distinct segments with correct tokens', () => {
    const { container } = renderCard();

    const segments = container.querySelectorAll('[class*="bg-"][aria-label]');
    const classMap = new Map<string, string>();
    segments.forEach((el) => {
      const label = el.getAttribute('aria-label') ?? '';
      classMap.set(label.split(':')[0], el.className);
    });

    expect(classMap.get('Available')).toContain('bg-success-strong');
    expect(classMap.get('Busy')).toContain('bg-warning-strong');
    expect(classMap.get('Reserved')).toContain('bg-info-strong');
    expect(classMap.get('Maintenance')).toContain('bg-neutral-strong');
    expect(classMap.get('Offline')).toContain('bg-danger-strong');
  });

  it('segment links use status query param', () => {
    renderCard();

    const links = screen.getAllByRole('link');
    const hrefs = links.map((l) => l.getAttribute('href') ?? '');
    expect(hrefs).toContain('/devices?status=available');
    expect(hrefs).toContain('/devices?status=busy');
    expect(hrefs).toContain('/devices?status=reserved');
    expect(hrefs).toContain('/devices?status=maintenance');
    expect(hrefs).toContain('/devices?status=offline');
  });
});

describe('FleetByPlatformCard with fleet health history', () => {
  it('renders the fleet health sparkline below platform chips', async () => {
    renderCard();

    expect(await screen.findByText(/^Fleet health$/i)).toBeInTheDocument();
    expect(
      await screen.findByLabelText(
        /Fleet health reachability over last 24 hours, currently 60 percent, average 60 percent/i,
      ),
    ).toBeInTheDocument();
  });

  it('uses the live fleet state for the current health point', async () => {
    mockUseDevices.mockReturnValue(
      makeQueryResult([
        device({ id: 'online-1', operational_state: 'available', hold: null }),
        device({ id: 'online-2', operational_state: 'busy', hold: null }),
      ]),
    );

    renderCard();

    expect(
      await screen.findByLabelText(
        /Fleet health reachability over last 24 hours, currently 100 percent, average 73 percent/i,
      ),
    ).toBeInTheDocument();
  });

  it('draws exact health point-to-point segments with rounded stroke joins', async () => {
    mockFleetTimeline = [
      { ...mockFleetTimeline[0]!, devices_total: 5, devices_offline: 4, devices_maintenance: 0 },
      { ...mockFleetTimeline[1]!, devices_total: 5, devices_offline: 4, devices_maintenance: 0 },
      { ...mockFleetTimeline[1]!, bucket_start: 't2', devices_total: 5, devices_offline: 1, devices_maintenance: 0 },
      { ...mockFleetTimeline[1]!, bucket_start: 't3', devices_total: 5, devices_offline: 2, devices_maintenance: 0 },
    ];
    const { container } = renderCard();

    await screen.findByText(/^Fleet health$/i);
    const linePath = container.querySelector('path[stroke-width="1.75"]');
    const d = linePath?.getAttribute('d') ?? '';

    expect(d).toContain(' L ');
    expect(d).not.toContain(' Q ');
    expect(d).not.toContain(' C ');
    expect(linePath?.getAttribute('stroke-linejoin')).toBe('round');
    expect(linePath?.getAttribute('stroke-linecap')).toBe('round');
  });
});

describe('FleetByPlatformCard Needs attention link', () => {
  it('shows Needs attention link when count > 0', () => {
    mockUseDevices.mockReturnValue(
      makeQueryResult([
        device({ id: 'a', operational_state: 'available', hold: null, needs_attention: true }),
        device({ id: 'b', operational_state: 'available', hold: null, needs_attention: false }),
      ]),
    );

    renderCard();

    expect(screen.getByRole('link', { name: /needs attention/i })).toBeInTheDocument();
  });

  it('omits Needs attention link when count is 0', () => {
    mockUseDevices.mockReturnValue(
      makeQueryResult([
        device({ id: 'a', operational_state: 'available', hold: null, needs_attention: false }),
        device({ id: 'b', operational_state: 'busy', hold: null, needs_attention: false }),
      ]),
    );

    renderCard();

    expect(screen.queryByRole('link', { name: /needs attention/i })).not.toBeInTheDocument();
  });
});

describe('FleetByPlatformCard driver pack warning', () => {
  it('shows warning when no driver packs are enabled', () => {
    mockUseDriverPackCatalog.mockReturnValue({ data: [] });

    renderCard();

    expect(screen.getByRole('alert')).toHaveTextContent(/no driver packs/i);
  });
});
