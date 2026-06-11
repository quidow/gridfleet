import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createReconnectingEventSource } from './reconnectingEventSource';

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
    const existing = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...existing, listener]);
  }

  close() {
    this.closed = true;
  }
}

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
}

describe('createReconnectingEventSource', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    MockEventSource.instances = [];
    vi.stubGlobal('EventSource', MockEventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('connects, registers listeners, and reports open', () => {
    const onOpen = vi.fn();
    const onMessage = vi.fn();
    createReconnectingEventSource({
      url: '/api/events',
      listeners: { 'device.health_changed': onMessage },
      onOpen,
    });
    const es = MockEventSource.instances[0];
    expect(es.url).toBe('/api/events');
    expect(es.listeners.get('device.health_changed')).toHaveLength(1);
    es.onopen?.();
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('reconnects with exponential backoff and resets delay after open', async () => {
    const onDisconnect = vi.fn();
    createReconnectingEventSource({
      url: '/api/events',
      listeners: {},
      onDisconnect,
      initialDelayMs: 1_000,
      maxDelayMs: 4_000,
    });
    MockEventSource.instances[0].onerror?.();
    await flushMicrotasks();
    expect(onDisconnect).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(MockEventSource.instances).toHaveLength(2);

    MockEventSource.instances[1].onerror?.();
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(2_000); // delay doubled
    expect(MockEventSource.instances).toHaveLength(3);

    MockEventSource.instances[2].onopen?.(); // open resets backoff
    MockEventSource.instances[2].onerror?.();
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(MockEventSource.instances).toHaveLength(4);
  });

  it('stops reconnecting when beforeReconnect returns false', async () => {
    createReconnectingEventSource({
      url: '/api/events',
      listeners: {},
      beforeReconnect: async () => false,
    });
    MockEventSource.instances[0].onerror?.();
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it('close() closes the source and cancels any pending reconnect', async () => {
    const handle = createReconnectingEventSource({ url: '/api/events', listeners: {} });
    MockEventSource.instances[0].onerror?.();
    await flushMicrotasks();
    handle.close();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].closed).toBe(true);
  });
});
