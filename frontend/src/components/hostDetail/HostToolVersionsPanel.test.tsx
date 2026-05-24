import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';
import { HostToolVersionsPanel } from './HostToolVersionsPanel';
import type { HostRead } from '../../types';

vi.mock('../../hooks/useHosts', () => ({
  useHostToolStatus: () => ({
    data: {
      host: {
        node: { name: 'node', version: '24.14.1', description: 'JavaScript runtime for Appium server' },
        node_provider: { name: 'node_provider', version: 'fnm', description: 'Node.js version manager' },
      },
      packs: {
        'appium-xcuitest': [
          { name: 'xcodebuild', version: '16.2', description: 'Builds and tests iOS/tvOS apps via Xcode' },
          { name: 'go_ios', version: '1.0.188', description: 'iOS real-device battery and hardware telemetry' },
        ],
        'appium-uiautomator2': [
          { name: 'adb', version: '35.0.2', description: 'Communicates with Android devices over USB and TCP' },
          { name: 'java', version: null, description: 'Required by UIAutomator2 test server build tools' },
        ],
      },
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

test('renders host tools section with node and node provider', () => {
  renderPanel();
  expect(screen.getByText('Host Tools')).toBeInTheDocument();
  expect(screen.getByText('node')).toBeInTheDocument();
  expect(screen.getByText('24.14.1')).toBeInTheDocument();
  expect(screen.getByText('node_provider')).toBeInTheDocument();
  expect(screen.getByText('fnm')).toBeInTheDocument();
});

test('renders driver pack dependencies grouped by pack', () => {
  renderPanel();
  expect(screen.getByText('Driver Pack Dependencies')).toBeInTheDocument();
  expect(screen.getByText('appium-xcuitest')).toBeInTheDocument();
  expect(screen.getByText('appium-uiautomator2')).toBeInTheDocument();
  expect(screen.getByText('xcodebuild')).toBeInTheDocument();
  expect(screen.getByText('16.2')).toBeInTheDocument();
  expect(screen.getByText('go_ios')).toBeInTheDocument();
  expect(screen.getByText('1.0.188')).toBeInTheDocument();
  expect(screen.getByText('adb')).toBeInTheDocument();
  expect(screen.getByText('35.0.2')).toBeInTheDocument();
});

test('shows descriptions for all tools', () => {
  renderPanel();
  expect(screen.getByText('JavaScript runtime for Appium server')).toBeInTheDocument();
  expect(screen.getByText('Builds and tests iOS/tvOS apps via Xcode')).toBeInTheDocument();
  expect(screen.getByText('iOS real-device battery and hardware telemetry')).toBeInTheDocument();
});

test('shows warning for missing tools', () => {
  renderPanel();
  expect(screen.getByText('not found')).toBeInTheDocument();
});
