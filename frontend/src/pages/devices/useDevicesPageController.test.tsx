import { describe, expect, it, vi, beforeEach } from 'vitest';
import type { ReactNode } from 'react';
import { renderHook } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useDevicesPageController } from './useDevicesPageController';
import type { DeviceRead } from '../../types';
import * as useDevicesModule from '../../hooks/useDevices';
import * as useHostsModule from '../../hooks/useHosts';

vi.mock('../../context/EventStreamContext', () => ({
  useEventStreamStatus: () => ({ connected: false }),
}));

function makeDevice(overrides: Partial<DeviceRead>): DeviceRead {
  return {
    id: overrides.id ?? `d-${Math.random()}`,
    availability_status: 'available',
    needs_attention: false,
    hardware_health_status: 'healthy',
    hardware_telemetry_state: 'fresh',
    name: 'X',
    pack_id: 'appium-uiautomator2',
    platform_id: 'android_mobile',
    platform_label: 'Android (real device)',
    os_version: '14',
    host_id: 'h',
    device_type: 'real_device',
    connection_type: 'usb',
    ...overrides,
  } as unknown as DeviceRead;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/devices?availability_status=available']}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe('useDevicesPageController', () => {
  beforeEach(() => {
    vi.spyOn(useHostsModule, 'useHosts').mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHostsModule.useHosts>);
  });

  it('summary stats reflect the full triage base, not the paginated page', () => {
    const triage: DeviceRead[] = [
      makeDevice({ id: 'a', availability_status: 'available' }),
      makeDevice({ id: 'b', availability_status: 'busy' }),
      makeDevice({ id: 'c', availability_status: 'offline', needs_attention: true }),
    ];
    const page = [triage[0]];

    vi.spyOn(useDevicesModule, 'useDevices').mockReturnValue({ data: triage } as unknown as ReturnType<typeof useDevicesModule.useDevices>);
    vi.spyOn(useDevicesModule, 'useDevicesPaginated').mockReturnValue({
      data: { items: page, total: 1, limit: 50, offset: 0 },
      isLoading: false,
      dataUpdatedAt: 0,
    } as unknown as ReturnType<typeof useDevicesModule.useDevicesPaginated>);

    const { result } = renderHook(() => useDevicesPageController(), { wrapper });

    expect(result.current.summaryStats.available).toBe(1);
    expect(result.current.summaryStats.busy).toBe(1);
    expect(result.current.summaryStats.offline).toBe(1);
    expect(result.current.summaryStats.attentionCount).toBe(1);
  });
});
