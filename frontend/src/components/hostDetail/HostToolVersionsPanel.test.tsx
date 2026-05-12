import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import HostToolVersionsPanel from './HostToolVersionsPanel';
import type { HostRead } from '../../types';

vi.mock('../../hooks/useHosts', () => ({
  useHostToolStatus: () => ({
    data: {
      node: '24.14.1',
      node_provider: 'fnm',
      go_ios: '1.0.188',
      appium: '3.3.0',
      selenium_jar: '4.41.0',
      selenium_jar_path: '/opt/gridfleet-agent/selenium-server.jar',
    },
    isLoading: false,
    error: null,
  }),
}));

vi.mock('../../hooks/useSettings', () => ({
  useSettings: () => ({ data: [] }),
}));

const host = {
  id: 'host-1',
  hostname: 'local-host',
  ip: '127.0.0.1',
  os_type: 'macos',
  agent_port: 5100,
  status: 'online',
  capabilities: {},
  missing_prerequisites: [],
  created_at: '2026-05-12T00:00:00Z',
  updated_at: '2026-05-12T00:00:00Z',
} as HostRead;

function renderPanel() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <HostToolVersionsPanel host={host} />
    </QueryClientProvider>,
  );
}

test('renders host-level tools without global appium or selenium jar management', () => {
  renderPanel();

  expect(screen.getByText('Node')).toBeInTheDocument();
  expect(screen.getByText('Node Provider')).toBeInTheDocument();
  expect(screen.getByText('go-ios')).toBeInTheDocument();
  expect(screen.queryByText('Appium')).not.toBeInTheDocument();
  expect(screen.queryByText('Selenium JAR')).not.toBeInTheDocument();
  expect(screen.queryByText('Selenium JAR Path')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /ensure versions/i })).not.toBeInTheDocument();
});
