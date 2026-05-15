import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import HostOverviewResourceStrip from './HostOverviewResourceStrip';

const sampleWithTotals = {
  timestamp: '2026-05-15T00:00:00Z',
  cpu_percent: 62,
  memory_used_mb: 14541,
  memory_total_mb: 32768,
  disk_used_gb: 410,
  disk_total_gb: 1024,
  disk_percent: 40,
};

vi.mock('../../hooks/useHosts', () => ({
  useHostResourceTelemetry: () => ({
    data: { samples: [sampleWithTotals] },
  }),
}));

function renderStrip(props: { hostId: string; totalMemoryMb: number | null; totalDiskGb: number | null }) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <HostOverviewResourceStrip {...props} />
    </QueryClientProvider>,
  );
}

test('renders used/total alongside percentages when telemetry totals exist', () => {
  renderStrip({ hostId: 'host-1', totalMemoryMb: 32768, totalDiskGb: 1024 });

  expect(screen.getByText(/14\.2\s*\/\s*32\.0\s*GB/i)).toBeInTheDocument();
  expect(screen.getByText(/410\s*\/\s*1024\s*GB/i)).toBeInTheDocument();
});
