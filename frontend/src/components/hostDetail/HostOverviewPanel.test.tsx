import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import HostOverviewPanel from './HostOverviewPanel';
import type { HostRead } from '../../types';

vi.mock('../../hooks/useHosts', () => ({
  useHostResourceTelemetry: () => ({
    data: {
      samples: [],
    },
  }),
  useHostToolStatus: () => ({
    data: {
      node: '24.14.1',
      node_provider: 'fnm',
      go_ios: '1.0.188',
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock('../hosts/hostPresentation', async () => {
  const actual = await vi.importActual<typeof import('../hosts/hostPresentation')>('../hosts/hostPresentation');
  return {
    ...actual,
    HostActionButtons: () => <button type="button">Discover Devices</button>,
  };
});

const host = {
  id: 'host-1',
  hostname: 'local-host',
  ip: '127.0.0.1',
  os_type: 'macos',
  agent_port: 5100,
  status: 'online',
  agent_version: '0.8.0',
  required_agent_version: null,
  recommended_agent_version: null,
  agent_update_available: false,
  agent_version_status: 'ok',
  capabilities: {
    platforms: ['legacy-platform'],
    tools: {
      adb: '1.0.41',
    },
  },
  missing_prerequisites: [],
  last_heartbeat: '2026-05-12T08:00:00Z',
  created_at: '2026-05-12T00:00:00Z',
  devices: [],
} as HostRead;

function renderOverview() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <HostOverviewPanel
        host={host}
        approvePending={false}
        rejectPending={false}
        discoverPending={false}
        onApprove={() => undefined}
        onReject={() => undefined}
        onDiscover={() => undefined}
      />
    </QueryClientProvider>,
  );
}

test('renders live tool versions on overview instead of static capabilities', () => {
  renderOverview();

  expect(screen.getByText('Tool Versions')).toBeInTheDocument();
  expect(screen.getByText('Node')).toBeInTheDocument();
  expect(screen.getByText('24.14.1')).toBeInTheDocument();
  expect(screen.queryByText('Capabilities')).not.toBeInTheDocument();
  expect(screen.queryByText('legacy-platform')).not.toBeInTheDocument();
  expect(screen.queryByText('adb')).not.toBeInTheDocument();
});

test('falls back to os_type and empty-glyph when hardware metadata is absent', () => {
  renderOverview();

  // OS row: no os_version on the fixture, so it falls back to os_type ("macos")
  expect(screen.getByText('macos')).toBeInTheDocument();

  // Each hardware-detail row in the Host Info <dl> renders the empty glyph in its <dd>
  for (const label of ['Kernel', 'Architecture', 'CPU', 'Cores']) {
    const term = screen.getByText(label, { selector: 'dt' });
    const row = term.parentElement;
    if (!row) throw new Error(`row for "${label}" not found`);
    expect(within(row).getByText('—')).toBeInTheDocument();
  }
});
