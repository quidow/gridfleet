import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import HostDetail from './HostDetail';

vi.mock('../hooks/usePageTitle', () => ({
  usePageTitle: () => undefined,
}));

vi.mock('../hooks/useHosts', () => ({
  useHost: () => ({
    data: {
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
      capabilities: {},
      missing_prerequisites: [],
      last_heartbeat: '2026-05-12T08:00:00Z',
      created_at: '2026-05-12T00:00:00Z',
      devices: [],
      os_version: 'macOS 14.5',
      kernel_version: 'Darwin 23.5.0',
      cpu_arch: 'arm64',
      cpu_model: 'Apple M2 Pro',
      cpu_cores: 12,
      total_memory_mb: 32768,
      total_disk_gb: 1024,
    },
    isLoading: false,
    error: null,
    dataUpdatedAt: 0,
  }),
  useHostDiagnostics: () => ({
    data: {
      host_id: 'host-1',
      circuit_breaker: {
        status: 'closed',
        consecutive_failures: 0,
        cooldown_seconds: 0,
        retry_after_seconds: null,
        probe_in_flight: false,
        last_error: null,
      },
      appium_processes: {
        reported_at: null,
        running_nodes: [],
      },
      recent_recovery_events: [],
    },
    isLoading: false,
    error: null,
  }),
  useHostCapabilities: () => ({
    data: { web_terminal_enabled: true },
  }),
  useHostResourceTelemetry: () => ({ data: { samples: [] } }),
  useApproveHost: () => ({ isPending: false, mutate: vi.fn() }),
  useRejectHost: () => ({ isPending: false, mutate: vi.fn() }),
}));

vi.mock('../components/hosts/useHostDiscoveryFlow', () => ({
  useHostDiscoveryFlow: () => ({
    discoverMut: { isPending: false },
    confirmMut: { isPending: false },
    discoveryResult: null,
    verifyDevice: null,
    closeDiscovery: vi.fn(),
    handleConfirm: vi.fn(),
    handleDiscover: vi.fn(),
    handleImportAndVerify: vi.fn(),
    toggleAdd: vi.fn(),
    toggleRemove: vi.fn(),
    selectedAddIdentities: new Set<string>(),
    selectedRemoveIdentities: new Set<string>(),
    setSelectedAddIdentities: vi.fn(),
    setSelectedRemoveIdentities: vi.fn(),
    setVerifyDevice: vi.fn(),
  }),
}));

vi.mock('../components/hostDetail/HostToolVersionsPanel', () => ({
  default: () => <section>Tool Versions</section>,
}));

vi.mock('../components/hostDetail/HostResourceTelemetryPanel', () => ({
  default: () => <section>Resource Telemetry</section>,
}));

vi.mock('../components/hostDetail/HostLogsPanel', () => ({
  default: ({ hostId }: { hostId: string }) => <section>Host logs for {hostId}</section>,
}));

function renderHostDetail(path: string) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/hosts/:id" element={<HostDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test('does not render tool versions on diagnostics tab', () => {
  renderHostDetail('/hosts/host-1?tab=diagnostics');

  expect(screen.getAllByText('Diagnostics').length).toBeGreaterThan(0);
  expect(screen.getByText('Resource Telemetry')).toBeInTheDocument();
  expect(screen.queryByText('Tool Versions')).not.toBeInTheDocument();
});

test('renders hardware fields on the overview tab', () => {
  renderHostDetail('/hosts/host-1?tab=overview');

  expect(screen.getByText('macOS 14.5')).toBeInTheDocument();
  expect(screen.getByText('Darwin 23.5.0')).toBeInTheDocument();
  expect(screen.getByText('arm64')).toBeInTheDocument();
  expect(screen.getByText('Apple M2 Pro')).toBeInTheDocument();
  expect(screen.getByText('12')).toBeInTheDocument();
});

test('renders host logs from query param', () => {
  renderHostDetail('/hosts/host-1?tab=logs&logs_tab=events');

  expect(screen.getByText('Host logs for host-1')).toBeInTheDocument();
});

test('switches to the host logs tab', () => {
  renderHostDetail('/hosts/host-1');

  fireEvent.click(screen.getByRole('button', { name: 'Logs' }));

  expect(screen.getByText('Host logs for host-1')).toBeInTheDocument();
});
