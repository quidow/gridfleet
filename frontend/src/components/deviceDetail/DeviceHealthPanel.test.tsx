import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DeviceHealthPanel } from './DeviceHealthPanel';
import { getCheckLabels } from './utils';
import type { DeviceHealth, DeviceLifecyclePolicy, PlatformConnectionBehavior, PlatformDescriptor } from '../../types';

const mutate = vi.fn();
let descriptor: PlatformDescriptor | null = null;

vi.mock('../../hooks/useDevices', () => ({
  useReconnectDevice: () => ({ mutate, isPending: false }),
  useRunDeviceSessionTest: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock('../../hooks/usePlatformDescriptor', () => ({
  usePlatformDescriptor: () => descriptor,
}));

function makeConnectionBehavior(
  overrides: Partial<PlatformConnectionBehavior> = {},
): PlatformConnectionBehavior {
  return {
    allow_transport_identity_until_host_resolution: false,
    requires_connection_target: true,
    requires_ip_address: false,
    ...overrides,
  };
}

function makeLifecyclePolicy(overrides: Partial<DeviceLifecyclePolicy> = {}): DeviceLifecyclePolicy {
  return {
    last_failure_source: null,
    last_failure_reason: null,
    last_action: null,
    last_action_at: null,
    deferred_stop: false,
    deferred_stop_reason: null,
    deferred_stop_since: null,
    excluded_from_run: false,
    excluded_run_id: null,
    excluded_run_name: null,
    excluded_at: null,
    will_auto_rejoin_run: false,
    recovery_suppressed_reason: null,
    backoff_until: null,
    recovery_state: 'idle',
    ...overrides,
  };
}

const baseHealth: DeviceHealth = {
  healthy: true,
  platform: 'generic_real',
  device_checks: {},
  node: { running: true, port: 4723, state: 'running' },
  session_viability: { status: null, last_attempted_at: null, last_succeeded_at: null, checked_by: null, error: null },
  lifecycle_policy: makeLifecyclePolicy(),
};

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DeviceHealthPanel
        health={baseHealth}
        packId="local/generic"
        platformId="generic_real"
        deviceType="real_device"
        connectionType="network"
        deviceId="device-1"
        canTestSession
        isLoading={false}
      />
    </QueryClientProvider>,
  );
}

describe('DeviceHealthPanel manifest actions', () => {
  it('renders manifest-labeled adapter checks from the checks array', () => {
    descriptor = {
      packId: 'appium-roku-dlenroc',
      platformId: 'roku_network',
      displayName: 'Roku',
      appiumPlatformName: 'roku',
      iconKind: 'tv',
      deviceTypes: ['real_device'],
      connectionTypes: ['network'],
      identityScheme: 'roku_serial',
      identityScope: 'global',
      lifecycleActions: [],
      healthChecks: [
        { id: 'ping', label: 'IP Reachable' },
        { id: 'ecp', label: 'ECP Reachable' },
      ],
      deviceFieldsSchema: [],
      defaultCapabilities: {},
      connectionBehavior: makeConnectionBehavior(),
      deviceTypeOverrides: {},
    };
    const health = {
      ...baseHealth,
      device_checks: {
        healthy: true,
        checks: [
          { check_id: 'ping', ok: true, message: '' },
          { check_id: 'ecp', ok: true, message: '' },
        ],
      },
    };
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <DeviceHealthPanel
          health={health}
          packId="appium-roku-dlenroc"
          platformId="roku_network"
          deviceType="real_device"
          connectionType="network"
          deviceId="device-1"
          canTestSession
          isLoading={false}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText('IP Reachable')).toBeInTheDocument();
    expect(screen.getByText('ECP Reachable')).toBeInTheDocument();
  });

  it('renders the summary status beside the section title', () => {
    descriptor = {
      packId: 'local/generic',
      platformId: 'generic_real',
      displayName: 'Generic',
      appiumPlatformName: 'Android',
      iconKind: 'generic',
      deviceTypes: ['real_device'],
      connectionTypes: ['network'],
      identityScheme: 'serial_number',
      identityScope: 'host',
      lifecycleActions: ['reconnect'],
      healthChecks: [],
      deviceFieldsSchema: [],
      defaultCapabilities: {},
      connectionBehavior: makeConnectionBehavior(),
      deviceTypeOverrides: {},
    };

    renderPanel();

    expect(screen.getByRole('heading', { name: 'Device Health' })).toBeInTheDocument();
    expect(screen.getByText('All checks passing')).toBeInTheDocument();
  });

  it('shows reconnect when the platform declares reconnect', () => {
    descriptor = {
      packId: 'local/generic',
      platformId: 'generic_real',
      displayName: 'Generic',
      appiumPlatformName: 'GenericTV',
      iconKind: 'generic',
      deviceTypes: ['real_device'],
      connectionTypes: ['network'],
      identityScheme: 'serial_number',
      identityScope: 'host',
      lifecycleActions: ['reconnect'],
      healthChecks: [],
      deviceFieldsSchema: [],
      defaultCapabilities: {},
      connectionBehavior: makeConnectionBehavior(),
      deviceTypeOverrides: {},
    };

    renderPanel();

    expect(screen.getByRole('button', { name: /reconnect device/i })).toBeInTheDocument();
  });

  it('hides reconnect when the platform does not declare reconnect', () => {
    descriptor = {
      packId: 'local/generic',
      platformId: 'generic_real',
      displayName: 'Generic',
      appiumPlatformName: 'Android',
      iconKind: 'generic',
      deviceTypes: ['real_device'],
      connectionTypes: ['network'],
      identityScheme: 'serial_number',
      identityScope: 'host',
      lifecycleActions: [],
      healthChecks: [],
      deviceFieldsSchema: [],
      defaultCapabilities: {},
      connectionBehavior: makeConnectionBehavior(),
      deviceTypeOverrides: {},
    };

    renderPanel();

    expect(screen.queryByRole('button', { name: /reconnect device/i })).not.toBeInTheDocument();
  });
});

describe('getCheckLabels', () => {
  const baseDescriptor: PlatformDescriptor = {
    packId: 'local/generic',
    platformId: 'generic_real',
    displayName: 'Generic',
    appiumPlatformName: 'GenericOS',
    iconKind: 'generic',
    deviceTypes: ['real_device'],
    connectionTypes: ['network'],
    identityScheme: 'generic_id',
    identityScope: 'host',
    lifecycleActions: [],
    healthChecks: [],
    deviceFieldsSchema: [],
    defaultCapabilities: {},
    connectionBehavior: makeConnectionBehavior(),
    deviceTypeOverrides: {},
  };

  it('uses descriptor health check labels', () => {
    expect(
      getCheckLabels({
        ...baseDescriptor,
        healthChecks: [{ id: 'socket_ready', label: 'Socket Ready' }],
      }),
    ).toEqual({ socket_ready: 'Socket Ready' });
  });

  it('does not infer platform health check labels', () => {
    expect(getCheckLabels({ ...baseDescriptor, appiumPlatformName: 'Android', healthChecks: [] })).toEqual({});
  });

  it('renders ip_ping health check with correct label', () => {
    expect(
      getCheckLabels({
        ...baseDescriptor,
        healthChecks: [{ id: 'ip_ping', label: 'ICMP Reachable' }],
      }),
    ).toEqual({ ip_ping: 'ICMP Reachable' });
  });
});
