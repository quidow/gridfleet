import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AddDeviceModal from './AddDeviceModal';
import type { IntakeCandidate } from '../../types';

const startVerification = vi.fn();
const mockUseDriverPackCatalog = vi.fn();
const mockUseIntakeCandidates = vi.fn();

class MockEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;

  addEventListener() {}
  removeEventListener() {}
  close() {}
}

function androidCatalog() {
  return {
    data: [
      {
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        state: 'enabled',
        current_release: '2026.04.0',
        runtime_policy: { strategy: 'recommended' },
        active_runs: 0,
        live_sessions: 0,
        platforms: [
          {
            id: 'android_mobile',
            display_name: 'Android',
            automation_name: 'UiAutomator2',
            appium_platform_name: 'Android',
            device_types: ['real_device', 'emulator'],
            connection_types: ['usb', 'network', 'virtual'],
            grid_slots: ['native'],
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            discovery_kind: 'adb',
            lifecycle_actions: [{ id: 'state' }, { id: 'reconnect' }],
            device_fields_schema: [],
            capabilities: {},
            display_metadata: { icon_kind: 'mobile' },
            default_capabilities: {},
            connection_behavior: {
              default_device_type: 'real_device',
              default_connection_type: 'usb',
              requires_connection_target: true,
              requires_ip_address: false,
            },
            device_type_overrides: {
              emulator: {
                lifecycle_actions: [{ id: 'state' }, { id: 'boot' }, { id: 'shutdown' }],
                connection_behavior: {
                  default_device_type: 'emulator',
                  default_connection_type: 'virtual',
                  requires_connection_target: true,
                  requires_ip_address: false,
                },
              },
            },
          },
        ],
      },
    ],
  };
}

function xcuitestCatalog() {
  return {
    data: [
      {
        id: 'appium-xcuitest',
        display_name: 'Appium XCUITest',
        state: 'enabled',
        current_release: '2026.04.0',
        runtime_policy: { strategy: 'recommended' },
        active_runs: 0,
        live_sessions: 0,
        platforms: [
          {
            id: 'tvos',
            display_name: 'tvOS',
            automation_name: 'XCUITest',
            appium_platform_name: 'tvOS',
            device_types: ['real_device', 'simulator'],
            connection_types: ['usb', 'network', 'virtual'],
            grid_slots: ['native'],
            identity_scheme: 'apple_udid',
            identity_scope: 'global',
            discovery_kind: 'apple',
            lifecycle_actions: [{ id: 'state' }, { id: 'reconnect' }],
            device_fields_schema: [],
            capabilities: {},
            display_metadata: { icon_kind: 'tv' },
            default_capabilities: {},
            connection_behavior: {
              default_device_type: 'real_device',
              default_connection_type: 'usb',
              requires_connection_target: true,
              requires_ip_address: false,
            },
            device_type_overrides: {
              real_device: {
                default_capabilities: {
                  'appium:platformVersion': '{device.os_version}',
                  'appium:usePreinstalledWDA': true,
                },
                device_fields_schema: [
                  {
                    id: 'wda_base_url',
                    label: 'WDA base URL',
                    type: 'network_endpoint',
                    required_for_session: true,
                    capability_name: 'appium:wdaBaseUrl',
                  },
                  {
                    id: 'use_preinstalled_wda',
                    label: 'Use pre-installed WDA',
                    type: 'bool',
                    default: true,
                    capability_name: 'appium:usePreinstalledWDA',
                  },
                  {
                    id: 'updated_wda_bundle_id',
                    label: 'Updated WDA bundle ID',
                    type: 'string',
                    required_for_session: true,
                    capability_name: 'appium:updatedWDABundleId',
                  },
                ],
                connection_behavior: {
                  default_device_type: 'real_device',
                  default_connection_type: 'usb',
                  requires_connection_target: true,
                  requires_ip_address: false,
                },
              },
              simulator: {
                identity: { scheme: 'simulator_udid', scope: 'host' },
                lifecycle_actions: [{ id: 'state' }, { id: 'boot' }, { id: 'shutdown' }],
                connection_behavior: {
                  default_device_type: 'simulator',
                  default_connection_type: 'virtual',
                  requires_connection_target: true,
                  requires_ip_address: false,
                },
              },
            },
          },
        ],
      },
    ],
  };
}

vi.mock('../../hooks/useDevices', () => ({
  useStartDeviceVerification: () => ({
    mutateAsync: startVerification,
    isPending: false,
    isError: false,
  }),
}));

vi.mock('../../hooks/useHosts', () => ({
  useIntakeCandidates: () => mockUseIntakeCandidates(),
}));

vi.mock('../../hooks/useDriverPacks', () => ({
  useDriverPackCatalog: () => mockUseDriverPackCatalog(),
}));

vi.mock('../../context/auth', () => ({
  useAuth: () => ({
    probeSession: vi.fn().mockResolvedValue({
      enabled: false,
      authenticated: false,
      username: null,
      csrf_token: null,
      expires_at: null,
    }),
  }),
}));

function renderModal() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AddDeviceModal
          isOpen
          onClose={vi.fn()}
          hostOptions={[{ id: 'host-1', name: 'Host 1' }]}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AddDeviceModal manual driver registration', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', MockEventSource);
    mockUseIntakeCandidates.mockReturnValue({ data: [], isFetching: false });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'local/generic-tv',
          display_name: 'Generic TV Driver',
          state: 'enabled',
          current_release: '1.0.0',
          runtime_policy: { strategy: 'recommended' },
          active_runs: 0,
          live_sessions: 0,
          platforms: [
            {
              id: 'generic_tv',
              display_name: 'Generic TV',
              automation_name: 'GenericAutomation',
              appium_platform_name: 'GenericTV',
              device_types: ['real_device'],
              connection_types: ['network'],
              grid_slots: ['native'],
              identity_scheme: 'generic_serial',
              identity_scope: 'host',
              discovery_kind: 'network_endpoint',
              lifecycle_actions: [{ id: 'state' }],
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'tv' },
              default_capabilities: {},
              connection_behavior: {
                default_device_type: 'real_device',
                default_connection_type: 'network',
                requires_connection_target: true,
                requires_ip_address: false,
              },
            },
          ],
        },
      ],
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    mockUseDriverPackCatalog.mockReset();
    mockUseIntakeCandidates.mockReset();
    startVerification.mockReset();
  });

  it('explains that observed devices are live and host-filtered', async () => {
    mockUseIntakeCandidates.mockReturnValue({ data: [], isFetching: true });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    expect(screen.getByText('Observed Devices')).toBeInTheDocument();
    expect(screen.getByText('Live')).toBeInTheDocument();
    expect(screen.getByText(/devices seen by the selected host/i)).toBeInTheDocument();
    expect(screen.getByText('No matching devices observed yet')).toBeInTheDocument();
  });

  it('shows an inline update note when observed devices change', async () => {
    let candidates: IntakeCandidate[] = [];
    mockUseIntakeCandidates.mockImplementation(() => ({ data: candidates, isFetching: false }));

    const { rerender } = renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');
    expect(screen.queryByText(/device list updated just now/i)).not.toBeInTheDocument();

    candidates = [
      {
        pack_id: 'local/generic-tv',
        platform_id: 'generic_tv',
        platform_label: 'Generic TV',
        identity_scheme: 'generic_serial',
        identity_scope: 'host',
        identity_value: 'serial-123',
        connection_target: '10.0.0.50:5555',
        name: 'Lab TV',
        os_version: '14',
        manufacturer: 'Generic',
        model: 'TV',
        detected_properties: null,
        device_type: 'real_device',
        connection_type: 'network',
        ip_address: '10.0.0.50',
        already_registered: false,
        registered_device_id: null,
      },
    ];
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <AddDeviceModal
            isOpen
            onClose={vi.fn()}
            hostOptions={[{ id: 'host-1', name: 'Host 1' }]}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText(/device list updated just now/i)).toBeInTheDocument();
    });
    expect(screen.getByText('1 device observed')).toBeInTheDocument();
  });

  it('requires an observed device for real-device USB and hides virtual/manual target choices', async () => {
    mockUseDriverPackCatalog.mockReturnValue(androidCatalog());

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    const connectionType = screen.getByLabelText(/connection type/i);
    expect(connectionType).toHaveDisplayValue('USB');
    expect(screen.getByRole('option', { name: 'USB' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Network' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Virtual' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/connection target/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /verify & add device/i })).toBeDisabled();
  });

  it('requires an observed emulator and hides manual connection target', async () => {
    mockUseDriverPackCatalog.mockReturnValue(androidCatalog());

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');
    await userEvent.selectOptions(screen.getByLabelText(/device type/i), 'emulator');

    expect(screen.queryByLabelText(/connection type/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/connection target/i)).not.toBeInTheDocument();
    expect(screen.getByText('No matching devices observed yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /verify & add device/i })).toBeDisabled();
  });

  it('collects the required tvOS WDA base URL from manifest fields', async () => {
    mockUseDriverPackCatalog.mockReturnValue(xcuitestCatalog());
    mockUseIntakeCandidates.mockReturnValue({
      data: [
        {
          pack_id: 'appium-xcuitest',
          platform_id: 'tvos',
          platform_label: 'tvOS',
          identity_scheme: 'apple_udid',
          identity_scope: 'global',
          identity_value: 'APPLE-TV-UDID',
          connection_target: 'APPLE-TV-UDID',
          name: 'Apple TV',
          os_version: '17.4',
          manufacturer: 'Apple',
          model: 'Apple TV 4K',
          detected_properties: null,
          device_type: 'real_device',
          connection_type: 'usb',
          ip_address: '192.168.1.70',
          already_registered: false,
          registered_device_id: null,
        },
      ],
      isFetching: false,
    });
    startVerification.mockResolvedValue({
      job_id: 'job-1',
      status: 'pending',
      current_stage: null,
      current_stage_status: null,
      detail: null,
      error: null,
      device_id: null,
      started_at: '2026-04-27T12:00:00Z',
      finished_at: null,
    });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');
    await userEvent.selectOptions(screen.getByLabelText(/observed device/i), 'APPLE-TV-UDID:APPLE-TV-UDID');
    expect(screen.getByLabelText('WDA base URL')).toBeRequired();
    expect(screen.getByLabelText('Updated WDA bundle ID')).toBeRequired();
    await userEvent.type(screen.getByLabelText('WDA base URL'), 'http://192.168.1.70');
    await userEvent.type(screen.getByLabelText('Updated WDA bundle ID'), 'com.test.WebDriverAgentRunner');
    await userEvent.click(screen.getByRole('button', { name: /verify & add device/i }));

    expect(startVerification).toHaveBeenCalledWith(
      expect.objectContaining({
        host_id: 'host-1',
        pack_id: 'appium-xcuitest',
        platform_id: 'tvos',
        identity_value: 'APPLE-TV-UDID',
        connection_target: 'APPLE-TV-UDID',
        device_config: expect.objectContaining({
          wda_base_url: 'http://192.168.1.70',
          use_preinstalled_wda: true,
          updated_wda_bundle_id: 'com.test.WebDriverAgentRunner',
        }),
      }),
    );
  });

  it('lets verification derive identity for a network_endpoint local driver', async () => {
    startVerification.mockResolvedValue({
      job_id: 'job-1',
      status: 'pending',
      current_stage: null,
      current_stage_status: null,
      detail: null,
      error: null,
      device_id: null,
      started_at: '2026-04-27T12:00:00Z',
      finished_at: null,
    });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    expect(screen.queryByLabelText(/identity value/i)).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/connection target/i), '10.0.0.50:5555');
    await userEvent.type(screen.getByLabelText(/display name/i), 'Lab TV');
    await userEvent.click(screen.getByRole('button', { name: /verify & add device/i }));

    expect(startVerification).toHaveBeenCalledWith(
      expect.objectContaining({
        host_id: 'host-1',
        pack_id: 'local/generic-tv',
        platform_id: 'generic_tv',
        identity_scheme: 'generic_serial',
        identity_value: null,
        connection_target: '10.0.0.50:5555',
        ip_address: '10.0.0.50',
        name: 'Lab TV',
      }),
    );
  });

  it('lets Roku registration derive identity from the IP address during verification', async () => {
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-roku-dlenroc',
          display_name: 'Roku (dlenroc)',
          state: 'enabled',
          current_release: '2026.04.5',
          runtime_policy: { strategy: 'recommended' },
          active_runs: 0,
          live_sessions: 0,
          platforms: [
            {
              id: 'roku_network',
              display_name: 'Roku (network)',
              automation_name: 'Roku',
              appium_platform_name: 'roku',
              device_types: ['real_device'],
              connection_types: ['network'],
              grid_slots: ['native'],
              identity_scheme: 'roku_serial',
              identity_scope: 'global',
              discovery_kind: 'network_endpoint',
              lifecycle_actions: [],
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'set_top' },
              default_capabilities: { 'appium:ip': '{device.ip_address}' },
              connection_behavior: {
                default_device_type: 'real_device',
                default_connection_type: 'network',
                requires_connection_target: false,
                requires_ip_address: true,
              },
            },
          ],
        },
      ],
    });
    startVerification.mockResolvedValue({
      job_id: 'job-1',
      status: 'pending',
      current_stage: null,
      current_stage_status: null,
      detail: null,
      error: null,
      device_id: null,
      started_at: '2026-04-27T12:00:00Z',
      finished_at: null,
    });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    expect(screen.queryByLabelText(/identity value/i)).not.toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/ip address/i), '192.168.1.50');
    await userEvent.click(screen.getByRole('button', { name: /verify & add device/i }));

    expect(startVerification).toHaveBeenCalledWith(
      expect.objectContaining({
        host_id: 'host-1',
        pack_id: 'appium-roku-dlenroc',
        platform_id: 'roku_network',
        identity_scheme: 'roku_serial',
        identity_value: null,
        connection_target: null,
        ip_address: '192.168.1.50',
      }),
    );
  });

  it('shows driver-pack warning instead of platform fields when no packs are enabled', async () => {
    mockUseDriverPackCatalog.mockReturnValue({ data: [] });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    expect(screen.getByText(/no driver packs are enabled/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /drivers/i })).toHaveAttribute('href', '/drivers');
    expect(screen.queryByLabelText(/platform/i)).not.toBeInTheDocument();
  });

  it('disambiguates duplicate platform labels with catalog device-type metadata', async () => {
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'local/mobile-pack',
          display_name: 'Local Mobile Pack',
          state: 'enabled',
          current_release: '1.0.0',
          runtime_policy: { strategy: 'recommended' },
          active_runs: 0,
          live_sessions: 0,
          platforms: [
            {
              id: 'generic_tv_real',
              display_name: 'Generic TV',
              automation_name: 'GenericAutomation',
              appium_platform_name: 'GenericTV',
              device_types: ['real_device'],
              connection_types: ['usb'],
              grid_slots: ['native'],
              identity_scheme: 'generic_serial',
              identity_scope: 'host',
              discovery_kind: 'adb',
              lifecycle_actions: [],
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'tv' },
              default_capabilities: {},
              connection_behavior: {},
            },
            {
              id: 'generic_tv_emulator',
              display_name: 'Generic TV',
              automation_name: 'GenericAutomation',
              appium_platform_name: 'GenericTV',
              device_types: ['emulator'],
              connection_types: ['virtual'],
              grid_slots: ['native'],
              identity_scheme: 'generic_serial',
              identity_scope: 'host',
              discovery_kind: 'adb',
              lifecycle_actions: [],
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'tv' },
              default_capabilities: {},
              connection_behavior: {},
            },
            {
              id: 'generic_tv_simulator',
              display_name: 'Generic TV',
              automation_name: 'GenericAutomation',
              appium_platform_name: 'GenericTV',
              device_types: ['simulator'],
              connection_types: ['virtual'],
              grid_slots: ['native'],
              identity_scheme: 'generic_serial',
              identity_scope: 'host',
              discovery_kind: 'adb',
              lifecycle_actions: [],
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'tv' },
              default_capabilities: {},
              connection_behavior: {},
            },
          ],
        },
      ],
    });

    renderModal();

    await userEvent.selectOptions(screen.getByLabelText(/host/i), 'host-1');

    expect(screen.getByRole('option', { name: 'Generic TV - Real Device' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Generic TV - Emulator' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Generic TV - Simulator' })).toBeInTheDocument();
  });
});
