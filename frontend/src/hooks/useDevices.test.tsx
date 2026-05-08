import { renderHook, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useDeviceTestData } from './useDevices';
import * as api from '../api/devices';

vi.mock('../api/devices');

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

it('useDeviceTestData fetches via api.getDeviceTestData', async () => {
  vi.mocked(api.getDeviceTestData).mockResolvedValueOnce({ k: 'v' });
  const { result } = renderHook(() => useDeviceTestData('abc'), { wrapper: wrap() });
  await waitFor(() => expect(result.current.data).toEqual({ k: 'v' }));
});
