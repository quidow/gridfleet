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

vi.mock('../../hooks/useDeviceDiagnostics', () => ({
  useExportDeviceDiagnostics: (id: string) => mockUseExport(id),
}));

describe('DiagnosticBundleButton', () => {
  beforeEach(() => {
    mockExport.mockReset();
    mockUseExport.mockClear();
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
});
