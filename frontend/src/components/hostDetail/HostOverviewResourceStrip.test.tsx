import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import HostOverviewResourceStrip, { formatCpuUsage } from './HostOverviewResourceStrip';

const sampleWithTotals = {
  timestamp: '2026-05-15T00:00:00Z',
  cpu_percent: 62,
  memory_used_mb: 14541,
  memory_total_mb: 32768,
  disk_used_gb: 410,
  disk_total_gb: 1024,
  disk_percent: 40,
};

const sampleWithoutTotals = {
  timestamp: '2026-05-15T00:00:00Z',
  cpu_percent: 62,
  memory_used_mb: null,
  memory_total_mb: null,
  disk_used_gb: null,
  disk_total_gb: null,
  disk_percent: 40,
};

const useHostResourceTelemetryMock = vi.fn();

vi.mock('../../hooks/useHosts', () => ({
  useHostResourceTelemetry: (...args: unknown[]) => useHostResourceTelemetryMock(...args),
}));

type RenderProps = {
  hostId: string;
  totalCpuCores: number | null;
  totalMemoryMb: number | null;
  totalDiskGb: number | null;
};

function renderStrip(props: RenderProps) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <HostOverviewResourceStrip {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useHostResourceTelemetryMock.mockReset();
  useHostResourceTelemetryMock.mockReturnValue({ data: { samples: [sampleWithTotals] } });
});

test('renders used/total alongside percentages when telemetry totals exist', () => {
  renderStrip({ hostId: 'host-1', totalCpuCores: 8, totalMemoryMb: 32768, totalDiskGb: 1024 });

  expect(screen.getByText(/14\.2\s*\/\s*32\.0\s*GB/i)).toBeInTheDocument();
  expect(screen.getByText(/410\s*\/\s*1024\s*GB/i)).toBeInTheDocument();
});

test('reserves detail row height when a gauge has no detail', () => {
  useHostResourceTelemetryMock.mockReturnValue({ data: { samples: [sampleWithoutTotals] } });

  const { container } = renderStrip({
    hostId: 'host-1',
    totalCpuCores: null,
    totalMemoryMb: null,
    totalDiskGb: null,
  });

  const detailRows = container.querySelectorAll('[data-testid="gauge-detail"]');
  expect(detailRows).toHaveLength(3);
  detailRows.forEach((row) => {
    expect(row.className).toMatch(/\bh-4\b/);
  });
});

describe('formatCpuUsage', () => {
  test('returns busy/total cores for valid inputs', () => {
    expect(formatCpuUsage(28, 8)).toBe('2.2 / 8 cores');
  });

  test('rounds busy cores to one decimal', () => {
    expect(formatCpuUsage(62, 8)).toBe('5.0 / 8 cores');
  });

  test('preserves saturation above 100%', () => {
    expect(formatCpuUsage(105, 8)).toBe('8.4 / 8 cores');
  });

  test('returns null when cpu_percent is null', () => {
    expect(formatCpuUsage(null, 8)).toBeNull();
  });

  test('returns null when cores is null', () => {
    expect(formatCpuUsage(28, null)).toBeNull();
  });

  test('returns null when cores is zero or negative', () => {
    expect(formatCpuUsage(28, 0)).toBeNull();
    expect(formatCpuUsage(28, -1)).toBeNull();
  });
});
