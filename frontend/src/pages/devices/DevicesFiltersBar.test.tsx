import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { DevicesFiltersBar } from './DevicesFiltersBar';

function renderBar(props: Partial<React.ComponentProps<typeof DevicesFiltersBar>> = {}) {
  const defaults: React.ComponentProps<typeof DevicesFiltersBar> = {
    packIdFilter: '',
    onPackIdFilterChange: vi.fn(),
    platformFilter: '',
    onPlatformFilterChange: vi.fn(),
    deviceTypeFilter: '',
    onDeviceTypeFilterChange: vi.fn(),
    connectionTypeFilter: '',
    onConnectionTypeFilterChange: vi.fn(),
    hardwareHealthStatusFilter: '',
    onHardwareHealthStatusFilterChange: vi.fn(),
    hardwareTelemetryStateFilter: '',
    onHardwareTelemetryStateFilterChange: vi.fn(),
    deviceHealthFilter: '',
    onDeviceHealthFilterChange: vi.fn(),
    nodeHealthFilter: '',
    onNodeHealthFilterChange: vi.fn(),
    viabilityFilter: '',
    onViabilityFilterChange: vi.fn(),
    osVersionFilter: '',
    onOsVersionFilterChange: vi.fn(),
    osVersions: [],
    search: '',
    onSearchChange: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <DevicesFiltersBar {...merged} />
    </QueryClientProvider>,
  );
  return merged;
}

describe('DevicesFiltersBar per-signal health filters', () => {
  it('renders the three verdict selects in the advanced section and fires the node handler', () => {
    // a set advanced filter opens the "More filters" section on mount
    const props = renderBar({ nodeHealthFilter: 'failed' });

    const nodeSelect = screen.getByLabelText('Filter by node health');
    expect(screen.getByLabelText('Filter by device health')).toBeInTheDocument();
    expect(screen.getByLabelText('Filter by viability')).toBeInTheDocument();

    fireEvent.change(nodeSelect, { target: { value: 'ok' } });
    expect(props.onNodeHealthFilterChange).toHaveBeenCalledWith('ok');
  });

  it('shows a removable chip when collapsed and a verdict filter is active', () => {
    const props = renderBar({ viabilityFilter: 'failed' });

    // collapse the advanced section to reveal chips
    fireEvent.click(screen.getByRole('button', { name: /more filters/i }));

    const chipRemove = screen.getByLabelText('Remove filter Viability: Failed');
    fireEvent.click(chipRemove);
    expect(props.onViabilityFilterChange).toHaveBeenCalledWith('');
  });
});
