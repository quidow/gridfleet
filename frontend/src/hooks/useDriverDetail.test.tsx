import { renderHook, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../api/driverPackDetail', () => ({
  deleteDriverPack: vi.fn().mockResolvedValue(undefined),
  setDriverPackCurrentRelease: vi.fn().mockResolvedValue({
    id: 'appium-uiautomator2',
    display_name: 'Appium UiAutomator2',
    state: 'enabled',
    current_release: '2026.04.1',
    platforms: [],
    runtime_policy: { strategy: 'recommended' },
    active_runs: 0,
    live_sessions: 0,
  }),
  fetchDriverPack: vi.fn().mockResolvedValue({
    id: 'appium-uiautomator2',
    display_name: 'Appium UiAutomator2',
    state: 'enabled',
    current_release: '2026.04.0',
    platforms: [],
    runtime_policy: { strategy: 'recommended' },
    active_runs: 0,
    live_sessions: 0,
  }),
  fetchDriverPackHosts: vi.fn().mockResolvedValue({
    pack_id: 'appium-uiautomator2',
    hosts: [{ host_id: 'host-1', hostname: 'android-host', status: 'online' }],
  }),
}));

import { useDeleteDriverPack, useDriverDetail, useDriverPackHosts, useSetDriverPackCurrentRelease } from './useDriverDetail';

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

it('fetches single driver pack', async () => {
  const { result } = renderHook(() => useDriverDetail('appium-uiautomator2'), { wrapper });
  await waitFor(() => expect(result.current.data).toBeDefined());
  expect(result.current.data?.id).toBe('appium-uiautomator2');
});

it('fetches driver pack host status', async () => {
  const { result } = renderHook(() => useDriverPackHosts('appium-uiautomator2'), { wrapper });
  await waitFor(() => expect(result.current.data).toBeDefined());
  expect(result.current.data?.hosts[0].hostname).toBe('android-host');
});

it('deletes a driver pack', async () => {
  const { result } = renderHook(() => useDeleteDriverPack(), { wrapper });

  result.current.mutate('appium-uiautomator2');

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
});

it('switches current driver pack release', async () => {
  const { result } = renderHook(() => useSetDriverPackCurrentRelease(), { wrapper });

  result.current.mutate({ packId: 'appium-uiautomator2', release: '2026.04.1' });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
});
