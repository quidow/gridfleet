import { describe, expect, it } from 'vitest';
import { deriveRunsSummary } from './runsSummaryDerivation';
import type { RunRead } from '../../types';

const baseRun = (overrides: Partial<RunRead>): RunRead => ({
  id: 'r',
  name: 'r',
  state: 'pending',
  requirements: [],
  ttl_minutes: 60,
  heartbeat_timeout_sec: 120,
  reserved_devices: null,
  error: null,
  created_at: '2026-04-19T10:00:00Z',
  started_at: null,
  completed_at: null,
  created_by: null,
  last_heartbeat: null,
  session_counts: { passed: 0, failed: 0, error: 0, running: 0, total: 0 },
  ...overrides,
});

describe('deriveRunsSummary', () => {
  const now = new Date('2026-04-19T12:00:00Z');

  it('counts running runs from active+completing states', () => {
    const runs = [
      baseRun({ id: 'a', state: 'active' }),
      baseRun({ id: 'b', state: 'completing' }),
      baseRun({ id: 'c', state: 'pending' }),
    ];
    expect(deriveRunsSummary(runs, now).running).toBe(2);
  });

  it('counts queued runs from pending+preparing states', () => {
    const runs = [
      baseRun({ id: 'a', state: 'pending' }),
      baseRun({ id: 'b', state: 'preparing' }),
      baseRun({ id: 'd', state: 'active' }),
    ];
    expect(deriveRunsSummary(runs, now).queued).toBe(2);
  });

  it('sums passed sessions only for runs completed in last 24h', () => {
    const runs = [
      baseRun({
        id: 'recent',
        state: 'completed',
        completed_at: '2026-04-19T08:00:00Z',
        session_counts: { passed: 12, failed: 0, error: 0, running: 0, total: 12 },
      }),
      baseRun({
        id: 'old',
        state: 'completed',
        completed_at: '2026-04-17T10:00:00Z',
        session_counts: { passed: 100, failed: 0, error: 0, running: 0, total: 100 },
      }),
    ];
    expect(deriveRunsSummary(runs, now).passed24h).toBe(12);
  });

  it('treats failed+error sessions as failed in 24h sum', () => {
    const runs = [
      baseRun({
        id: 'r',
        state: 'failed',
        completed_at: '2026-04-19T11:00:00Z',
        session_counts: { passed: 0, failed: 2, error: 3, running: 0, total: 5 },
      }),
    ];
    expect(deriveRunsSummary(runs, now).failed24h).toBe(5);
  });

  it('returns zeros for empty input', () => {
    expect(deriveRunsSummary([], now)).toEqual({ running: 0, queued: 0, passed24h: 0, failed24h: 0 });
  });
});
