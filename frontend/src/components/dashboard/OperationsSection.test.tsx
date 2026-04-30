import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { TrendingUp } from 'lucide-react';
import OperationsSection, { MetricTile } from './OperationsSection';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('../../hooks/useRuns', () => ({
  useRuns: () => ({ data: [], status: 'success', isError: false, refetch: vi.fn() }),
}));
vi.mock('../../hooks/useDevices', () => ({
  useDevices: () => ({ data: [], status: 'success', isError: false, refetch: vi.fn() }),
}));
vi.mock('../../hooks/useAnalytics', () => ({
  useFleetOverview: () => ({
    data: { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 },
    isError: false,
    refetch: vi.fn(),
  }),
}));
vi.mock('../../hooks/useRetriableQueryState', () => ({
  deriveRetriableQueryState: () => 'success',
}));
vi.mock('../../hooks/useSessionsDaily', () => ({
  useSessionsDaily: () => ({ series: [], data: undefined, isError: false, refetch: vi.fn() }),
}));

function renderTile(value: string | number) {
  return render(
    <MemoryRouter>
      <MetricTile icon={TrendingUp} label="Pass rate" value={value} to="/analytics" tone="neutral" />
    </MemoryRouter>,
  );
}

describe('MetricTile', () => {
  it('renders numeric value with mono + tabular', () => {
    renderTile('92%');
    const el = screen.getByText('92%');
    expect(el.className).toMatch(/font-mono/);
    expect(el.className).toMatch(/tabular-nums/);
  });

  it('renders non-numeric placeholder without mono treatment', () => {
    renderTile('No runs');
    const el = screen.getByText('No runs');
    expect(el.className).not.toMatch(/font-mono/);
    expect(el.className).not.toMatch(/metric-numeric/);
  });

  it('em-dash is treated as non-numeric', () => {
    renderTile('—');
    const el = screen.getByText('—');
    expect(el.className).not.toMatch(/font-mono/);
  });

  it('plain integer is numeric', () => {
    renderTile(7);
    const el = screen.getByText('7');
    expect(el.className).toMatch(/font-mono/);
  });
});

describe('OperationsSection empty state', () => {
  it('shows per-list idle cells while keeping the section layout stable', () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <OperationsSection />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/No active runs/i)).toBeInTheDocument();
    expect(screen.getByText(/No busy devices/i)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Active runs/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Busy devices/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Last 7 days/i })).toBeInTheDocument();
  });
});
