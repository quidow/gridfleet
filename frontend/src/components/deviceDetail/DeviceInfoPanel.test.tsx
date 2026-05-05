import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect } from 'vitest';
import DeviceInfoPanel from './DeviceInfoPanel';
import type { DeviceDetail } from '../../types';

const deviceFixture: Partial<DeviceDetail> = {
  id: 'd1',
  pack_id: 'appium-uiautomator2',
  platform_id: 'android_mobile',
  platform_label: 'Android Emulator',
  identity_scheme: 'manager_generated',
  identity_scope: 'host',
  identity_value: 'avd:Pixel_6',
  connection_target: 'Pixel_6',
  device_type: 'emulator',
  connection_type: 'virtual',
  ip_address: null,
  os_version: 'API 37',
  operational_state: 'offline', hold: null,
  host_id: 'host-uuid-abc',
  tags: {},
  manufacturer: null,
  model: null,
  reservation: null,
  created_at: '2026-04-01T00:00:00Z',
  updated_at: '2026-04-01T00:00:00Z',
};

describe('DeviceInfoPanel host row', () => {
  it('renders hostLabel when provided', () => {
    render(
      <MemoryRouter>
        <DeviceInfoPanel device={deviceFixture as DeviceDetail} hostLabel="lab-mac-01.local" />
      </MemoryRouter>,
    );
    expect(screen.getByText('lab-mac-01.local')).toBeInTheDocument();
    expect(screen.queryByText('host-uuid-abc')).toBeNull();
  });

  it('falls back to host_id when hostLabel not supplied', () => {
    render(
      <MemoryRouter>
        <DeviceInfoPanel device={deviceFixture as DeviceDetail} />
      </MemoryRouter>,
    );
    expect(screen.getByText('host-uuid-abc')).toBeInTheDocument();
  });

  it('renders Identity label as DT inside DefinitionList', () => {
    render(
      <MemoryRouter>
        <DeviceInfoPanel device={deviceFixture as DeviceDetail} />
      </MemoryRouter>,
    );
    const dt = screen.getByText('Identity');
    expect(dt.tagName).toBe('DT');
  });

  it('renders manufacturer and model rows', () => {
    const enriched: Partial<DeviceDetail> = {
      ...deviceFixture,
      manufacturer: 'Google',
      model: 'Pixel 8',
      model_number: 'GKWS6',
      software_versions: {
        android: '14',
        build: 'AP1A.240405.002',
      },
    };
    render(
      <MemoryRouter>
        <DeviceInfoPanel device={enriched as DeviceDetail} />
      </MemoryRouter>,
    );
    expect(screen.getByText('Manufacturer')).toBeInTheDocument();
    expect(screen.getByText('Google')).toBeInTheDocument();
    expect(screen.getByText('Model Name')).toBeInTheDocument();
    expect(screen.getByText('Pixel 8')).toBeInTheDocument();
    expect(screen.getByText('Model Number')).toBeInTheDocument();
    expect(screen.getByText('GKWS6')).toBeInTheDocument();
    expect(screen.getByText('Software Versions')).toBeInTheDocument();
    expect(screen.getByText('Android')).toBeInTheDocument();
    expect(screen.getByText('14')).toBeInTheDocument();
    expect(screen.getByText('Build')).toBeInTheDocument();
    expect(screen.getByText('AP1A.240405.002')).toBeInTheDocument();
  });

  it('renders user-defined tags as compact device info', () => {
    render(
      <MemoryRouter>
        <DeviceInfoPanel
          device={
            {
              ...deviceFixture,
              tags: { owner: 'qa', room: 'lab-1' },
            } as DeviceDetail
          }
        />
      </MemoryRouter>,
    );

    expect(screen.getByText('Tags')).toBeInTheDocument();
    expect(screen.getByText('owner: qa')).toBeInTheDocument();
    expect(screen.getByText('room: lab-1')).toBeInTheDocument();
  });

  it('renders an empty tags row without routing label copy', () => {
    render(
      <MemoryRouter>
        <DeviceInfoPanel device={deviceFixture as DeviceDetail} />
      </MemoryRouter>,
    );

    expect(screen.getByText('Tags')).toBeInTheDocument();
    expect(screen.queryByText('Routing labels')).not.toBeInTheDocument();
    expect(screen.queryByText(/No routing labels/i)).not.toBeInTheDocument();
  });
});
