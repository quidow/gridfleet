export const POLL_FAST_MS = 5_000;
export const POLL_DEFAULT_MS = 10_000;
export const POLL_RELAXED_MS = 15_000;
export const POLL_SLOW_MS = 30_000;

const SSE_SAFETY_NET_MS = 60_000;

export function sseAdaptivePolling(connected: boolean, intervalMs: number, safetyNetMs = SSE_SAFETY_NET_MS) {
  const refetchInterval = connected ? safetyNetMs : intervalMs;
  return { refetchInterval, staleTime: refetchInterval / 2 };
}
