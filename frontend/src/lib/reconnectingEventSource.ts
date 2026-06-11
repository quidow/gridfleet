export type ReconnectingEventSourceHandle = { close(): void };

type ReconnectingEventSourceOptions = {
  url: string;
  listeners: Record<string, (event: MessageEvent) => void>;
  onOpen?: () => void;
  onDisconnect?: () => void;
  /** Async gate run after a connection error, before scheduling a reconnect. Return false to stop. */
  beforeReconnect?: () => Promise<boolean>;
  initialDelayMs?: number;
  maxDelayMs?: number;
};

export function createReconnectingEventSource({
  url,
  listeners,
  onOpen,
  onDisconnect,
  beforeReconnect,
  initialDelayMs = 1_000,
  maxDelayMs = 30_000,
}: ReconnectingEventSourceOptions): ReconnectingEventSourceHandle {
  let disposed = false;
  let eventSource: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let delay = initialDelayMs;

  function closeSource() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function connect() {
    if (disposed) {
      return;
    }
    closeSource();

    const es = new EventSource(url);
    eventSource = es;

    es.onopen = () => {
      delay = initialDelayMs;
      onOpen?.();
    };

    es.onerror = () => {
      if (disposed) {
        return;
      }
      onDisconnect?.();
      closeSource();
      void (async () => {
        const shouldReconnect = beforeReconnect ? await beforeReconnect() : true;
        if (disposed || !shouldReconnect) {
          return;
        }
        reconnectTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, maxDelayMs);
      })();
    };

    for (const [type, handler] of Object.entries(listeners)) {
      es.addEventListener(type, handler);
    }
  }

  connect();

  return {
    close() {
      disposed = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
      }
      closeSource();
    },
  };
}
