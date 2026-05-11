import { act, renderHook, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useClearAppiumNodeTransition, useDeviceTestData } from './useDevices';
import * as api from '../api/devices';

vi.mock('../api/devices');
vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
  },
}));

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

it('useClearAppiumNodeTransition reports mutation errors', async () => {
  vi.mocked(api.clearAppiumNodeTransition).mockRejectedValueOnce(new Error('not allowed'));
  const { result } = renderHook(() => useClearAppiumNodeTransition(), { wrapper: wrap() });

  await act(async () => {
    await expect(result.current.mutateAsync({ nodeId: 'node-1', reason: 'stuck' })).rejects.toThrow('not allowed');
  });

  expect(toast.error).toHaveBeenCalledWith('not allowed');
});
