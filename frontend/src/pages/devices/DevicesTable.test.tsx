import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import DevicesTable from './DevicesTable';
import type { DeviceRead } from '../../types';

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  const base: DeviceRead = {
    id: 'd1',
    name: 'Pixel-7',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: null,
    identity_scheme: 'adb_serial',
    identity_scope: 'global' as const,
    manufacturer: 'Google',
    model: 'Pixel 7',
    os_version: '14',
    device_type: 'real_device',
    connection_type: 'usb',
    host_id: 'h1',
    operational_state: 'available', hold: null,
    needs_attention: false,
    readiness_state: 'verified',
    auto_manage: true,
    emulator_state: null,
    reservation: null,
    missing_setup_fields: [],
    tags: {},
    identity_value: 'emulator-5554',
    connection_target: null,
    ip_address: null,
    battery_level_percent: null,
    battery_temperature_c: null,
    charging_state: null,
    hardware_health_status: 'unknown',
    hardware_telemetry_reported_at: null,
    hardware_telemetry_state: 'unknown',
    verified_at: null,
    lifecycle_policy_summary: {
      state: 'idle',
      label: 'Normal',
      detail: null,
      backoff_until: null,
    },
    health_summary: {
      healthy: null,
      summary: 'No data',
      last_checked_at: null,
    },
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  };
  return { ...base, ...overrides };
}

function renderTable(props: Partial<React.ComponentProps<typeof DevicesTable>> = {}) {
  const defaults = {
    devices: [makeDevice()],
    selectedIds: new Set<string>(),
    hostMap: new Map([['h1', 'host-01']]),
    sort: { key: 'name' as const, direction: 'asc' as const },
    pendingActionForDevice: () => null,
    onSortChange: vi.fn(),
    onToggleSelectAll: vi.fn(),
    onToggleSelect: vi.fn(),
    onAction: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  return {
    ...render(
      <MemoryRouter>
        <DevicesTable {...merged} />
      </MemoryRouter>,
    ),
    props: merged,
  };
}

describe('DevicesTable', () => {
  it('renders a device row with the device name', () => {
    renderTable();
    expect(screen.getByRole('link', { name: 'Pixel-7' })).toBeInTheDocument();
  });

  it('fires onToggleSelect when the row checkbox is clicked', () => {
    const { props } = renderTable();
    fireEvent.click(screen.getByLabelText('Select row 1'));
    expect(props.onToggleSelect).toHaveBeenCalledWith('d1');
  });

  it('fires onToggleSelectAll when the header checkbox is clicked', () => {
    const { props } = renderTable();
    fireEvent.click(screen.getByLabelText('Select all rows'));
    expect(props.onToggleSelectAll).toHaveBeenCalled();
  });

  it('fires onSortChange with desc when the Device header is clicked (already asc)', () => {
    const { props } = renderTable();
    fireEvent.click(screen.getByRole('button', { name: /^Device/i }));
    expect(props.onSortChange).toHaveBeenCalledWith({ key: 'name', direction: 'desc' });
  });

  it('fires onSortChange with asc when the Platform header is clicked', () => {
    const { props } = renderTable();
    fireEvent.click(screen.getByRole('button', { name: /^Platform/i }));
    expect(props.onSortChange).toHaveBeenCalledWith({ key: 'platform', direction: 'asc' });
  });

  it('fires toggle-auto-manage action when the toggle is clicked', () => {
    const { props } = renderTable();
    fireEvent.click(screen.getByLabelText('Toggle auto-manage for Pixel-7'));
    expect(props.onAction).toHaveBeenCalledWith({
      type: 'toggle-auto-manage',
      deviceId: 'd1',
      autoManage: false,
    });
  });

  it('marks row as selected when id is in selectedIds', () => {
    renderTable({ selectedIds: new Set(['d1']) });
    const row = screen.getByTestId('device-row-d1');
    expect(row.className).toContain('bg-accent-soft');
  });

  it('renders Offline badge for offline + needs_attention device', () => {
    renderTable({
      devices: [
        makeDevice({
          operational_state: 'offline', hold: null,
          needs_attention: true,
          lifecycle_policy_summary: {
            state: 'suppressed',
            label: 'Suppressed',
            detail: null,
            backoff_until: null,
          },
          readiness_state: 'verified',
        }),
      ],
    });
    expect(screen.getByText('Offline')).toBeInTheDocument();
  });

  it('opens state details for a device with active cooldown', () => {
    renderTable({
      devices: [
        makeDevice({
          reservation: {
            run_id: 'run-1',
            run_name: 'Cooldown Run',
            run_state: 'active',
            excluded: true,
            exclusion_reason: 'appium launch timeout',
            excluded_until: '2026-05-03T20:00:00Z',
            cooldown_remaining_sec: 42,
          },
        }),
      ],
    });

    const trigger = screen.getByRole('button', { name: 'State details for Pixel-7' });
    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });
});
