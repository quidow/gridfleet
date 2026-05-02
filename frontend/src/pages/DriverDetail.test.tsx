import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, vi } from 'vitest';

const deletePackMutate = vi.fn();
const setCurrentReleaseMutate = vi.fn();

const basePack = {
  id: 'appium-uiautomator2',
  display_name: 'Appium UiAutomator2',
  maintainer: 'gridfleet-team',
  license: 'Apache-2.0',
  state: 'enabled',
  current_release: '2026.04.0',
  platforms: [
    {
      id: 'android_mobile',
      display_name: 'Android (real device)',
      automation_name: 'UiAutomator2',
      appium_platform_name: 'Android',
      device_types: ['real_device'],
      connection_types: ['usb', 'network'],
      grid_slots: ['native', 'chrome'],
      identity_scheme: 'android_serial',
      identity_scope: 'host',
      device_fields_schema: [
        {
          id: 'roku_password',
          label: 'Developer password',
          type: 'string',
          required_for_session: true,
          sensitive: true,
        },
      ],
      capabilities: {
        stereotype: { 'appium:platformName': 'Android' },
        session_required: ['appium:appPackage'],
      },
      default_capabilities: { 'appium:systemPort': 8200 },
      connection_behavior: {
        default_device_type: 'real_device',
        default_connection_type: 'usb',
        requires_connection_target: true,
      },
      parallel_resources: {
        ports: [{ capability_name: 'appium:systemPort', start: 8200 }],
        derived_data_path: false,
      },
      health_checks: [{ id: 'adb_connected', label: 'ADB Connected' }],
      lifecycle_actions: [{ id: 'reconnect' }],
    },
  ],
  appium_server: {
    source: 'npm',
    package: 'appium',
    version: '>=2.5,<3',
    recommended: '2.11.5',
    known_bad: [],
    github_repo: null,
  },
  appium_driver: {
    source: 'npm',
    package: 'appium-uiautomator2-driver',
    version: '>=3,<5',
    recommended: '3.6.0',
    known_bad: ['4.0.0'],
    github_repo: null,
  },
  insecure_features: ['uiautomator2:chromedriver_autodownload'],
  workarounds: [
    {
      id: 'android_host_resolution',
      applies_when: { platform_ids: ['android_mobile'] },
      env: { APPIUM_TEST_FLAG: '1' },
    },
  ],
  doctor: [{ id: 'adb', description: 'ADB available', adapter_hook: 'check_adb' }],
  features: {},
  runtime_summary: {
    installed_hosts: 1,
    blocked_hosts: 0,
    actual_appium_server_versions: ['2.19.0'],
    actual_appium_driver_versions: ['4.2.0'],
    driver_drift_hosts: 1,
  },
  runtime_policy: { strategy: 'recommended' },
  active_runs: 0,
  live_sessions: 0,
};

let mockPack = basePack;

vi.mock('../hooks/useDriverDetail', () => ({
  useDriverDetail: () => ({
    data: mockPack,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useDriverReleases: () => ({
    data: {
      pack_id: 'appium-uiautomator2',
      releases: [
        {
          release: '2026.04.1',
          is_current: true,
          artifact_sha256: 'sha-new',
          created_at: '2026-04-28T00:00:00Z',
          platform_ids: ['android_mobile'],
        },
        {
          release: '2026.04.0',
          is_current: false,
          artifact_sha256: 'sha-old',
          created_at: '2026-04-27T00:00:00Z',
          platform_ids: ['android_mobile'],
        },
      ],
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useDeleteDriverPack: () => ({
    mutate: deletePackMutate,
    isPending: false,
    error: null,
  }),
  useSetDriverPackCurrentRelease: () => ({
    mutate: setCurrentReleaseMutate,
    isPending: false,
    error: null,
  }),
  useDriverPackHosts: () => ({
    data: {
      pack_id: 'appium-uiautomator2',
      hosts: [
        {
          host_id: 'host-1',
          hostname: 'android-host.local',
          status: 'online',
          pack_release: '2026.04.0',
          runtime_id: 'runtime-1',
          pack_status: 'installed',
          resolved_install_spec: { appium_server: 'appium@2.11.5', appium_driver_version: '3.6.0' },
          installer_log_excerpt: '',
          resolver_version: 'resolver-1',
          blocked_reason: null,
          installed_at: '2026-04-28T00:00:00Z',
          desired_appium_driver_version: '3.6.0',
          installed_appium_driver_version: '3.6.0',
          appium_driver_drift: false,
          appium_home: '/opt/gridfleet-agent/runtime-1',
          runtime_status: 'installed',
          runtime_blocked_reason: null,
          appium_server_version: '2.19.0',
          doctor: [{ check_id: 'adb', ok: true, message: 'ok' }],
        },
      ],
    },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

vi.mock('../hooks/useDriverPacks', () => ({
  useSetDriverPackState: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock('../api/driverPackAuthoring', () => ({
  exportPack: vi.fn(),
}));

import DriverDetail from './DriverDetail';

beforeEach(() => {
  deletePackMutate.mockReset();
  setCurrentReleaseMutate.mockReset();
  mockPack = basePack;
  vi.restoreAllMocks();
});

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/drivers/appium-uiautomator2']}>
        <Routes>
          <Route path="/drivers/:id" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

it('renders pack name in header', () => {
  render(<DriverDetail />, { wrapper });
  expect(screen.getByText('Appium UiAutomator2')).toBeInTheDocument();
});

it('renders redesigned tabs and hides operations when no operations exist', () => {
  render(<DriverDetail />, { wrapper });
  expect(screen.getByRole('button', { name: /overview/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /platforms/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /runtime/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /hosts/i })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /manifest/i })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /features/i })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /operations/i })).not.toBeInTheDocument();
});

it('renders overview live rollout signals without manifest identity details', () => {
  render(<DriverDetail />, { wrapper });
  expect(screen.getByText('Live Rollout')).toBeInTheDocument();
  expect(screen.getByText('server actual 2.19.0')).toBeInTheDocument();
  expect(screen.getByText('driver actual 4.2.0')).toBeInTheDocument();
  expect(screen.queryByText('gridfleet-team')).not.toBeInTheDocument();
  expect(screen.queryByText('Apache-2.0')).not.toBeInTheDocument();
  expect(screen.queryByText('appium-uiautomator2-driver')).not.toBeInTheDocument();
  expect(screen.queryByText('>=3,<5')).not.toBeInTheDocument();
});

it('renders platform manifest details', async () => {
  render(<DriverDetail />, { wrapper });
  await userEvent.click(screen.getByRole('button', { name: /platforms/i }));
  expect(screen.getByText('Android (real device)')).toBeInTheDocument();
  expect(screen.getByText('appium:appPackage')).toBeInTheDocument();
  expect(screen.getAllByText('appium:systemPort: 8200').length).toBeGreaterThan(0);
  expect(screen.getByText('Developer password *')).toBeInTheDocument();
});

it('renders state badge', () => {
  render(<DriverDetail />, { wrapper });
  expect(screen.getByText('enabled')).toBeInTheDocument();
});

it('confirms before deleting the driver pack', async () => {
  render(<DriverDetail />, { wrapper });

  await userEvent.click(screen.getByRole('button', { name: /delete/i }));
  const dialog = screen.getByRole('dialog', { name: /delete driver pack/i });
  expect(dialog).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole('button', { name: /^delete$/i }));

  expect(deletePackMutate).toHaveBeenCalledWith('appium-uiautomator2', expect.any(Object));
});

it('renders runtime manifest details', async () => {
  render(<DriverDetail />, { wrapper });
  await userEvent.click(screen.getByRole('button', { name: /runtime/i }));
  expect(screen.getByText('Manifest Runtime Contract')).toBeInTheDocument();
  expect(screen.getByText('Desired Appium Server')).toBeInTheDocument();
  expect(screen.getByText('Desired Appium Driver')).toBeInTheDocument();
  expect(screen.getByText('uiautomator2:chromedriver_autodownload')).toBeInTheDocument();
  expect(screen.getByText('android_host_resolution')).toBeInTheDocument();
  expect(screen.getByText('ADB available')).toBeInTheDocument();
});

it('renders hosts tab with pack installation status', async () => {
  render(<DriverDetail />, { wrapper });
  await userEvent.click(screen.getByRole('button', { name: /hosts/i }));
  expect(screen.getByText('android-host.local')).toBeInTheDocument();
  expect(screen.getByText('runtime-1')).toBeInTheDocument();
  expect(screen.getByText('actual appium@2.19.0')).toBeInTheDocument();
  expect(screen.getByText('desired appium@2.11.5')).toBeInTheDocument();
  expect(screen.getByText('installed')).toBeInTheDocument();
});

it('switches to an older uploaded release from the releases tab', async () => {
  render(<DriverDetail />, { wrapper });

  await userEvent.click(screen.getByRole('button', { name: /releases/i }));
  await userEvent.click(screen.getByRole('button', { name: /switch to 2026.04.0/i }));

  expect(setCurrentReleaseMutate).toHaveBeenCalledWith({
    packId: 'appium-uiautomator2',
    release: '2026.04.0',
  });
});

it('renders operations tab only when manifest features exist', async () => {
  mockPack = {
    ...basePack,
    features: {
      remotexpc_tunnel: {
        display_name: 'RemoteXPC tunnel',
        description_md: 'Keeps iOS 18 real-device tunnels available.',
        actions: [{ id: 'restart', label: 'Restart tunnel' }],
      },
    },
  };

  render(<DriverDetail />, { wrapper });
  await userEvent.click(screen.getByRole('button', { name: /operations/i }));
  expect(screen.getByText('RemoteXPC tunnel')).toBeInTheDocument();
  expect(screen.getByText('Restart tunnel')).toBeInTheDocument();
});
