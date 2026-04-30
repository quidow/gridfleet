import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { useEventStream } from './useEventStream';
import type { AuthSession } from '../types';

const invalidateQueries = vi.fn().mockResolvedValue(undefined);
const probeSession = vi.fn<() => Promise<AuthSession>>();
const authValue = { probeSession };
const EVENT_CATALOG = [
  { name: 'session.started' },
  { name: 'run.created' },
  { name: 'device.availability_changed' },
];

vi.mock('../api/settings', () => ({
  fetchSettings: vi.fn().mockResolvedValue([]),
}));

vi.mock('./useEventCatalog', () => ({
  useEventCatalog: () => ({
    data: EVENT_CATALOG,
  }),
}));

vi.mock('../context/auth', () => ({
  useAuth: () => authValue,
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    error: vi.fn(),
  },
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

  emit(type: string, payload: Record<string, unknown>) {
    const listeners = this.listeners.get(type) ?? [];
    for (const listener of listeners) {
      listener({ data: JSON.stringify({ data: payload }) } as MessageEvent);
    }
  }

  close() {
    this.closed = true;
  }
}

function HookProbe() {
  const { connected } = useEventStream();
  return <div>{connected ? 'connected' : 'disconnected'}</div>;
}

function renderHookProbe() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  vi.spyOn(queryClient, 'invalidateQueries').mockImplementation(invalidateQueries);

  return render(
    <QueryClientProvider client={queryClient}>
      <HookProbe />
    </QueryClientProvider>,
  );
}

describe('useEventStream', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('EventSource', MockEventSource);
    MockEventSource.instances = [];
    invalidateQueries.mockClear();
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

  it('batches high-volume invalidations into a single 3-second flush window', async () => {
    renderHookProbe();
    const source = MockEventSource.instances.at(-1)!;
    await act(async () => {
      source.onopen?.();
    });
    expect(screen.getByText('connected')).toBeInTheDocument();

    await act(async () => {
      for (let index = 0; index < 100; index += 1) {
        source.emit('session.started', { index });
      }
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_999);
    });
    expect(invalidateQueries).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(invalidateQueries).toHaveBeenCalledTimes(8);
  });

  it('uses exponential reconnect backoff and resets after a successful reconnect', async () => {
    renderHookProbe();
    const initialCount = MockEventSource.instances.length;
    const initial = MockEventSource.instances.at(-1)!;

    await act(async () => {
      initial.onerror?.();
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(MockEventSource.instances).toHaveLength(initialCount);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockEventSource.instances).toHaveLength(initialCount + 1);

    const second = MockEventSource.instances.at(-1)!;
    await act(async () => {
      second.onerror?.();
      await vi.advanceTimersByTimeAsync(1_999);
    });
    expect(MockEventSource.instances).toHaveLength(initialCount + 1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockEventSource.instances).toHaveLength(initialCount + 2);

    const third = MockEventSource.instances.at(-1)!;
    await act(async () => {
      third.onopen?.();
    });
    expect(screen.getByText('connected')).toBeInTheDocument();
    await act(async () => {
      third.onerror?.();
      await vi.advanceTimersByTimeAsync(999);
    });

    expect(MockEventSource.instances).toHaveLength(initialCount + 2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(MockEventSource.instances).toHaveLength(initialCount + 3);
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
    const initialCount = MockEventSource.instances.length;
    const source = MockEventSource.instances.at(-1)!;

    await act(async () => {
      source.onerror?.();
      await Promise.resolve();
    });

    expect(probeSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(MockEventSource.instances).toHaveLength(initialCount);
  });
});
