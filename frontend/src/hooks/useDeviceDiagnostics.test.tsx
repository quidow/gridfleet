import { renderHook, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const fetchSnapshot = vi.fn().mockResolvedValue({
  snapshot_id: 'snap-1',
  device_id: 'dev-1',
  captured_at: '2026-05-23T00:00:00Z',
  payload: {},
});

vi.mock('../api/deviceDiagnostics', () => ({
  fetchDeviceDiagnosticSnapshot: (...args: unknown[]) => fetchSnapshot(...args),
  listDeviceDiagnosticSnapshots: vi.fn(),
  exportDeviceDiagnostics: vi.fn(),
}));

import { useDeviceDiagnosticSnapshot } from './useDeviceDiagnostics';

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

it('treats snapshot detail as immutable — no scheduled polling, infinite stale time', async () => {
  const qc = makeClient();
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );

  const { result } = renderHook(
    () => useDeviceDiagnosticSnapshot('dev-1', 'snap-1', false),
    { wrapper },
  );
  await waitFor(() => expect(result.current.data).toBeDefined());
  expect(fetchSnapshot).toHaveBeenCalledTimes(1);

  const [observer] = qc
    .getQueryCache()
    .find({ queryKey: ['device-diagnostic-snapshot', 'dev-1', 'snap-1', false] })!
    .observers;
  const options = observer.options;
  expect(options.refetchInterval).toBe(false);
  expect(options.staleTime).toBe(Infinity);
  expect(result.current.isStale).toBe(false);
});
