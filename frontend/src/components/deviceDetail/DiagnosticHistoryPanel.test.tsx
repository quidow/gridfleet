import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DiagnosticHistoryPanel } from './DiagnosticHistoryPanel';

const mockList = vi.fn();
const mockDetail = vi.fn();

vi.mock('../../hooks/useDeviceDiagnostics', () => ({
  useDeviceDiagnosticSnapshots: (deviceId: string, limit: number) => mockList(deviceId, limit),
  useDeviceDiagnosticSnapshot: (deviceId: string, snapshotId: string | null, redact: boolean) =>
    mockDetail(deviceId, snapshotId, redact),
}));

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiagnosticHistoryPanel deviceId="dev-1" />
    </QueryClientProvider>,
  );
}

describe('DiagnosticHistoryPanel', () => {
  beforeEach(() => {
    mockList.mockReset();
    mockDetail.mockReset();
  });

  it('renders an empty state when no snapshots exist', () => {
    mockList.mockReturnValue({ data: { items: [], next_before: null }, isLoading: false });
    mockDetail.mockReturnValue({ data: undefined, isLoading: false });
    renderPanel();
    expect(screen.getByText(/no diagnostic snapshots/i)).toBeInTheDocument();
  });

  it('renders trigger badge and timestamp for each snapshot', () => {
    mockList.mockReturnValue({
      data: {
        items: [
          { id: 's1', captured_at: '2026-05-17T10:00:00Z', trigger: 'operator', reason: null },
          {
            id: 's2',
            captured_at: '2026-05-17T09:00:00Z',
            trigger: 'review_required',
            reason: 'health_failure',
          },
        ],
        next_before: null,
      },
      isLoading: false,
    });
    mockDetail.mockReturnValue({ data: undefined, isLoading: false });
    renderPanel();
    expect(screen.getByText(/Operator/)).toBeInTheDocument();
    expect(screen.getByText(/Auto: review/)).toBeInTheDocument();
    expect(screen.getByText(/health_failure/)).toBeInTheDocument();
  });

  it('opens the detail modal when a row is clicked', async () => {
    mockList.mockReturnValue({
      data: {
        items: [{ id: 's1', captured_at: '2026-05-17T10:00:00Z', trigger: 'operator', reason: null }],
        next_before: null,
      },
      isLoading: false,
    });
    mockDetail.mockReturnValue({
      data: {
        id: 's1',
        captured_at: '2026-05-17T10:00:00Z',
        trigger: 'operator',
        reason: null,
        payload: { schema_version: 1, hi: 'snap' },
      },
      isLoading: false,
    });
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByRole('button', { name: /open snapshot s1/i }));
    await waitFor(() => {
      expect(screen.getByText(/"schema_version": 1/)).toBeInTheDocument();
    });
  });
});
