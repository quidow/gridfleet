import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import DiagnosticBundleButton from './DiagnosticBundleButton';

const mockExport = vi.fn();
const mockUseExport = vi.fn(() => ({
  mutateAsync: mockExport,
  isPending: false,
}));
const mockFetchSnapshot = vi.fn();

vi.mock('../../hooks/useDeviceDiagnostics', () => ({
  useExportDeviceDiagnostics: (id: string) => mockUseExport(id),
}));

vi.mock('../../api/deviceDiagnostics', () => ({
  fetchDeviceDiagnosticSnapshot: (...args: unknown[]) => mockFetchSnapshot(...args),
}));

describe('DiagnosticBundleButton', () => {
  beforeEach(() => {
    mockExport.mockReset();
    mockUseExport.mockClear();
    mockFetchSnapshot.mockReset();
  });

  it('opens the modal with the captured payload', async () => {
    mockExport.mockResolvedValueOnce({
      payload: { schema_version: 1, hi: 'there' },
      snapshot_id: 'abc',
      warnings: [],
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <DiagnosticBundleButton deviceId="dev-1" />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: /capture diagnostic bundle/i }));
    await waitFor(() => {
      expect(screen.getByText(/"schema_version": 1/)).toBeInTheDocument();
    });
  });

  it('re-fetches the same snapshot when toggling redaction (no new POST)', async () => {
    mockExport.mockResolvedValueOnce({
      payload: { schema_version: 1, device: { identity_value: 'serial-001' } },
      snapshot_id: 'snap-1',
      warnings: [],
    });
    mockFetchSnapshot.mockResolvedValueOnce({
      id: 'snap-1',
      captured_at: '2026-05-18T00:00:00Z',
      trigger: 'operator',
      reason: null,
      payload: { schema_version: 1, redacted: true, device: { identity_value: 'redacted:abcd1234' } },
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <DiagnosticBundleButton deviceId="dev-1" />
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole('button', { name: /capture diagnostic bundle/i }));
    await waitFor(() => {
      expect(screen.getByText(/serial-001/)).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /^redact$/i }));
    await waitFor(() => {
      expect(screen.getByText(/redacted:abcd1234/)).toBeInTheDocument();
    });
    expect(mockFetchSnapshot).toHaveBeenCalledWith('dev-1', 'snap-1', { redact: true });
    // POST export must NOT have been called a second time.
    expect(mockExport).toHaveBeenCalledTimes(1);
  });
});
