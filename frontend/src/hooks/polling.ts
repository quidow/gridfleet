const SSE_SAFETY_NET_MS = 60_000;

export function sseAdaptivePolling(connected: boolean, intervalMs: number, safetyNetMs = SSE_SAFETY_NET_MS) {
  const refetchInterval = connected ? safetyNetMs : intervalMs;
  return { refetchInterval, staleTime: refetchInterval / 2 };
}
