import { describe, expect, it } from 'vitest';
import { deriveHostResourceTelemetryState } from './hostResourceTelemetry';

describe('host resource telemetry freshness', () => {
  it('returns unknown when no sample exists', () => {
    expect(deriveHostResourceTelemetryState(null, 60)).toBe('unknown');
  });

  it('returns fresh when the latest sample is within two intervals', () => {
    const now = Date.parse('2026-04-16T10:00:00Z');
    expect(deriveHostResourceTelemetryState('2026-04-16T09:58:30Z', 60, now)).toBe('fresh');
  });

  it('returns stale when the latest sample is older than two intervals', () => {
    const now = Date.parse('2026-04-16T10:00:00Z');
    expect(deriveHostResourceTelemetryState('2026-04-16T09:57:30Z', 60, now)).toBe('stale');
  });
});
