import { act, renderHook, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useDeviceTestData, useRunDeviceSessionTest } from './useDevices';
import * as api from '../api/devices';

vi.mock('../api/devices');
vi.mock('sonner', () => ({
  toast: {
    error: vi.fn(),
    message: vi.fn(),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

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

it('useRunDeviceSessionTest surfaces a 409 as an informational notice, not an error', async () => {
  vi.mocked(api.runDeviceSessionTest).mockRejectedValueOnce(
    Object.assign(new Error('Session viability check already in progress for this device'), { status: 409 }),
  );
  const { result } = renderHook(() => useRunDeviceSessionTest(), { wrapper: wrap() });

  await act(async () => {
    await expect(result.current.mutateAsync('dev-1')).rejects.toThrow();
  });

  expect(toast.message).toHaveBeenCalledWith(
    'A session probe is already running for this device — try again in a moment.',
  );
  expect(toast.error).not.toHaveBeenCalled();
});

it('useRunDeviceSessionTest reports non-409 failures as errors', async () => {
  vi.mocked(api.runDeviceSessionTest).mockRejectedValueOnce(
    Object.assign(new Error('node unreachable'), { status: 502 }),
  );
  const { result } = renderHook(() => useRunDeviceSessionTest(), { wrapper: wrap() });

  await act(async () => {
    await expect(result.current.mutateAsync('dev-1')).rejects.toThrow('node unreachable');
  });

  expect(toast.error).toHaveBeenCalledWith('node unreachable');
});
