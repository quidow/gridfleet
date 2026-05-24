import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { HostOverviewResourceStrip } from './HostOverviewResourceStrip';
import { formatCpuUsage } from './hostResourceFormatters';

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

const { useHostResourceTelemetryMock } = vi.hoisted(() => ({
  useHostResourceTelemetryMock: vi.fn(),
}));

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

test('renders CPU core count when totalCpuCores is provided', () => {
  useHostResourceTelemetryMock.mockReturnValue({ data: { samples: [sampleWithTotals] } });

  renderStrip({ hostId: 'host-1', totalCpuCores: 8, totalMemoryMb: 32768, totalDiskGb: 1024 });

  expect(screen.getByText('8 cores')).toBeInTheDocument();
});

test('CPU detail is absent (but row reserved) when totalCpuCores is null', () => {
  useHostResourceTelemetryMock.mockReturnValue({ data: { samples: [sampleWithTotals] } });

  const { container } = renderStrip({
    hostId: 'host-1',
    totalCpuCores: null,
    totalMemoryMb: 32768,
    totalDiskGb: 1024,
  });

  expect(screen.queryByText(/cores/i)).not.toBeInTheDocument();
  const detailRows = container.querySelectorAll('[data-testid="gauge-detail"]');
  expect(detailRows).toHaveLength(3);
  detailRows.forEach((row) => expect(row.className).toMatch(/\bh-4\b/));
});

describe('formatCpuUsage', () => {
  test('returns core count for valid inputs', () => {
    expect(formatCpuUsage(28, 8)).toBe('8 cores');
  });

  test('returns same core count regardless of cpu percent', () => {
    expect(formatCpuUsage(62, 8)).toBe('8 cores');
    expect(formatCpuUsage(105, 8)).toBe('8 cores');
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

  test('returns null for non-finite or negative cpu_percent', () => {
    expect(formatCpuUsage(Number.NaN, 8)).toBeNull();
    expect(formatCpuUsage(Number.POSITIVE_INFINITY, 8)).toBeNull();
    expect(formatCpuUsage(Number.NEGATIVE_INFINITY, 8)).toBeNull();
    expect(formatCpuUsage(-0.1, 8)).toBeNull();
  });
});
