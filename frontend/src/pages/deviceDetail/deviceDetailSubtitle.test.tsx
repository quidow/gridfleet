import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import type { DeviceDetail } from '../../types';
import { buildDeviceDetailSubtitleNode } from './deviceDetailSubtitle';

function baseDevice(overrides: Partial<DeviceDetail> = {}): DeviceDetail {
  return {
    platform_id: 'android_mobile',
    platform_label: null,
    os_version: 'API 37',
    host_id: 'host-01',
    emulator_state: null,
    reservation: null,
    ...overrides,
  } as DeviceDetail;
}

describe('buildDeviceDetailSubtitleNode', () => {
  it('renders "<platform> · <os_version> · <host>" meta text for a fully populated device', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice(), 'host-friendly.local')}</p>
      </MemoryRouter>,
    );
    expect(screen.getByText('Android Mobile · API 37 · host-friendly.local')).toBeInTheDocument();
  });

  it('drops the os_version segment when absent', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice({ os_version: null }), 'host-01')}</p>
      </MemoryRouter>,
    );
    expect(screen.getByText('Android Mobile · host-01')).toBeInTheDocument();
  });

  it('falls back to host_id when hostLabel is null', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice(), null)}</p>
      </MemoryRouter>,
    );
    expect(screen.getByText('Android Mobile · API 37 · host-01')).toBeInTheDocument();
  });

  it('uses catalog platform_label when provided', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice({ platform_id: 'custom_platform', platform_label: 'My Custom Platform' }), 'host-01')}</p>
      </MemoryRouter>,
    );
    expect(screen.getByText('My Custom Platform · API 37 · host-01')).toBeInTheDocument();
  });

  it('renders the EmulatorStateBadge when emulator_state is set', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice({ emulator_state: 'stopped' }), 'host-01')}</p>
      </MemoryRouter>,
    );
    expect(screen.getByTestId('emulator-state-badge')).toBeInTheDocument();
  });

  it('omits the EmulatorStateBadge when emulator_state is null', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice(), 'host-01')}</p>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId('emulator-state-badge')).not.toBeInTheDocument();
  });

  it('renders the ReservationPill when reservation is set', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(
          baseDevice({
            reservation: { run_id: 'r1', run_name: 'n1', excluded: false } as DeviceDetail['reservation'],
          }),
          'host-01',
        )}</p>
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: /Reserved by n1/ })).toBeInTheDocument();
  });

  it('omits the ReservationPill when reservation is null', () => {
    render(
      <MemoryRouter>
        <p>{buildDeviceDetailSubtitleNode(baseDevice(), 'host-01')}</p>
      </MemoryRouter>,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
});
