import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession, DeviceVerificationJob, PlatformDescriptor } from '../../types';
import {
  getAllowedConnectionTypes,
  getAllowedDeviceTypes,
  manualRegistrationRequirements,
  normalizeFormForDescriptor,
  useDeviceVerificationJobController,
} from './deviceVerificationWorkflow';

const fetchDeviceVerificationJob = vi.fn();
const onClose = vi.fn();
const probeSession = vi.fn<() => Promise<AuthSession>>();
const authValue = { probeSession };

vi.mock('../../api/devices', () => ({
  fetchDeviceVerificationJob: (...args: unknown[]) => fetchDeviceVerificationJob(...args),
}));

vi.mock('../../context/auth', () => ({
  useAuth: () => authValue,
}));

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    const current = this.listeners.get(type) ?? [];
    current.push(listener);
    this.listeners.set(type, current);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void) {
    const current = this.listeners.get(type) ?? [];
    this.listeners.set(type, current.filter((entry) => entry !== listener));
  }

  emit(type: string, payload: DeviceVerificationJob) {
    const listeners = this.listeners.get(type) ?? [];
    for (const listener of listeners) {
      listener({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }

  close() {
    this.closed = true;
  }
}

const INITIAL_JOB: DeviceVerificationJob = {
  job_id: 'job-1',
  status: 'pending',
  current_stage: null,
  current_stage_status: null,
  detail: null,
  error: null,
  device_id: null,
  started_at: '2026-03-30T10:00:00Z',
  finished_at: null,
};

const androidMobileReal: PlatformDescriptor = {
  packId: 'appium-uiautomator2',
  platformId: 'android_mobile',
  displayName: 'Android',
  appiumPlatformName: 'Android',
  iconKind: 'mobile',
  deviceTypes: ['real_device', 'emulator'],
  connectionTypes: ['usb', 'network', 'virtual'],
  identityScheme: 'android_serial',
  identityScope: 'host',
  lifecycleActions: ['state', 'reconnect'],
  healthChecks: [],
  deviceFieldsSchema: [],
  defaultCapabilities: {},
  connectionBehavior: {},
};

const genericNetworkEndpoint: PlatformDescriptor = {
  packId: 'local/generic-tv',
  platformId: 'generic_tv',
  displayName: 'Generic TV',
  appiumPlatformName: 'GenericTV',
  iconKind: 'tv',
  deviceTypes: ['real_device'],
  connectionTypes: ['network'],
  identityScheme: 'generic_serial',
  identityScope: 'host',
  lifecycleActions: ['state'],
  healthChecks: [],
  deviceFieldsSchema: [],
  defaultCapabilities: {},
  connectionBehavior: {
    default_device_type: 'real_device',
    default_connection_type: 'network',
    requires_connection_target: true,
    requires_ip_address: false,
  },
};

const rokuNetworkEndpoint: PlatformDescriptor = {
  ...genericNetworkEndpoint,
  packId: 'appium-roku-dlenroc',
  platformId: 'roku_network',
  displayName: 'Roku',
  appiumPlatformName: 'roku',
  identityScheme: 'roku_serial',
  identityScope: 'global',
  connectionBehavior: {
    default_device_type: 'real_device',
    default_connection_type: 'network',
    requires_connection_target: false,
    requires_ip_address: true,
  },
};

describe('descriptor-backed verification helpers', () => {
  it('derives allowed device types from descriptor', () => {
    expect(getAllowedDeviceTypes(androidMobileReal)).toEqual(['real_device', 'emulator']);
  });

  it('returns no device types when descriptor missing', () => {
    expect(getAllowedDeviceTypes(null)).toEqual([]);
  });

  it('derives connection types from descriptor', () => {
    expect(getAllowedConnectionTypes(androidMobileReal, 'real_device')).toEqual(['usb', 'network']);
    expect(getAllowedConnectionTypes(androidMobileReal, 'emulator')).toEqual(['virtual']);
  });

  it('normalizes form using descriptor field defaults', () => {
    const tvosReal: PlatformDescriptor = {
      ...androidMobileReal,
      platformId: 'tvos',
      iconKind: 'tv',
      deviceTypes: ['real_device'],
      connectionTypes: ['network'],
      deviceFieldsSchema: [
        { id: 'use_preinstalled_wda', label: 'WDA', type: 'bool', default: true },
      ],
    };

    const form = normalizeFormForDescriptor({ platform_id: 'tvos' } as never, tvosReal);

    expect((form.device_config as Record<string, unknown>)?.use_preinstalled_wda).toBe(true);
    expect(form.device_type).toBe('real_device');
    expect(form.connection_type).toBe('network');
  });

  it('requires connection target but not manual identity from manifest behavior', () => {
    expect(manualRegistrationRequirements(genericNetworkEndpoint, 'real_device', 'network')).toEqual({
      identity: false,
      connectionTarget: true,
      ipAddress: false,
    });
  });

  it('requires observed devices instead of manual targets for usb and virtual lanes', () => {
    expect(manualRegistrationRequirements(androidMobileReal, 'real_device', 'usb')).toEqual({
      identity: false,
      connectionTarget: false,
      ipAddress: false,
    });
    expect(manualRegistrationRequirements(androidMobileReal, 'emulator', 'virtual')).toEqual({
      identity: false,
      connectionTarget: false,
      ipAddress: false,
    });
  });

  it('lets endpoint-only packs derive identity from the host adapter', () => {
    expect(manualRegistrationRequirements(rokuNetworkEndpoint, 'real_device', 'network')).toEqual({
      identity: false,
      connectionTarget: false,
      ipAddress: true,
    });
  });

});

function HookProbe() {
  const [job, setJob] = useState<DeviceVerificationJob | null>(INITIAL_JOB);
  const { activeJob } = useDeviceVerificationJobController({
    isOpen: true,
    job,
    onJobChange: setJob,
    onClose,
  });

  return (
    <div>
      <span>{activeJob?.status ?? 'none'}</span>
      <span>{activeJob?.current_stage ?? 'idle'}</span>
      <span>{activeJob?.detail ?? 'no-detail'}</span>
    </div>
  );
}

function renderHookProbe() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <HookProbe />
    </QueryClientProvider>,
  );
}

describe('useDeviceVerificationJobController', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', MockEventSource);
    MockEventSource.instances = [];
    fetchDeviceVerificationJob.mockReset();
    onClose.mockReset();
    probeSession.mockReset();
    probeSession.mockResolvedValue({
      enabled: false,
      authenticated: false,
      username: null,
      csrf_token: null,
      expires_at: null,
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('uses the dedicated verification stream and refetches once before reconnecting', async () => {
    fetchDeviceVerificationJob.mockResolvedValue({
      ...INITIAL_JOB,
      status: 'running',
      current_stage: 'cleanup',
      current_stage_status: 'running',
      detail: 'Re-synced after reconnect',
    });

    renderHookProbe();
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0]?.url).toBe('/api/devices/verification-jobs/job-1/events');

    await act(async () => {
      MockEventSource.instances[0]?.emit('device.verification.updated', {
        ...INITIAL_JOB,
        status: 'running',
        current_stage: 'session_probe',
        current_stage_status: 'running',
        detail: 'Streaming verification update',
      });
    });

    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.getByText('session_probe')).toBeInTheDocument();
    expect(screen.getByText('Streaming verification update')).toBeInTheDocument();

    await act(async () => {
      MockEventSource.instances[0]?.onerror?.();
    });

    expect(fetchDeviceVerificationJob).toHaveBeenCalledWith('job-1');
    expect(screen.getByText('cleanup')).toBeInTheDocument();
    expect(screen.getByText('Re-synced after reconnect')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(MockEventSource.instances).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1]?.url).toBe('/api/devices/verification-jobs/job-1/events');
  });

  it('stops reconnecting when the browser session has expired', async () => {
    probeSession.mockResolvedValueOnce({
      enabled: true,
      authenticated: false,
      username: null,
      csrf_token: null,
      expires_at: null,
    });

    renderHookProbe();

    await act(async () => {
      MockEventSource.instances[0]?.onerror?.();
      await Promise.resolve();
    });

    expect(probeSession).toHaveBeenCalledWith();
    expect(fetchDeviceVerificationJob).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(MockEventSource.instances).toHaveLength(1);
  });
});
