import { describe, expect, it } from 'vitest';
import type { SessionRead } from '../../types/devices';
import {
  failures7d,
  lastSession,
  passRate7d,
  sessions24h,
} from './deviceStatStripSummary';

function makeSession(overrides: Partial<SessionRead>): SessionRead {
  return {
    id: overrides.id ?? 'sess-1',
    session_id: overrides.session_id ?? 'sid-1',
    test_name: overrides.test_name ?? null,
    started_at: overrides.started_at ?? new Date().toISOString(),
    ended_at: overrides.ended_at ?? null,
    status: overrides.status ?? 'passed',
    requested_pack_id: null,
    requested_platform_id: null,
    requested_device_type: null,
    requested_connection_type: null,
    requested_capabilities: null,
    error_type: null,
    error_message: null,
    run_id: null,
  };
}

describe('deviceStatStripSummary', () => {
  const now = new Date('2026-04-20T12:00:00Z');

  it('sessions24h counts sessions started within 24h', () => {
    const sessions = [
      makeSession({ id: 's1', started_at: '2026-04-20T11:00:00Z' }),
      makeSession({ id: 's2', started_at: '2026-04-20T00:00:00Z' }),
      makeSession({ id: 's3', started_at: '2026-04-19T12:00:00Z' }),
      makeSession({ id: 's4', started_at: '2026-04-18T12:00:00Z' }),
    ];
    expect(sessions24h(sessions, now)).toBe(3);
  });

  it('sessions24h returns 0 for empty input', () => {
    expect(sessions24h([], now)).toBe(0);
  });

  it('sessions24h excludes future-dated sessions', () => {
    const sessions = [
      makeSession({ id: 'future', started_at: '2026-05-01T12:00:00Z' }),
      makeSession({ id: 'recent', started_at: '2026-04-20T11:00:00Z' }),
    ];
    expect(sessions24h(sessions, now)).toBe(1);
  });

  it('passRate7d returns null when no sessions in window', () => {
    expect(passRate7d([], now)).toBeNull();
  });

  it('passRate7d computes integer percent of passed over 7d', () => {
    const sessions = [
      makeSession({ id: 'p1', status: 'passed', started_at: '2026-04-19T12:00:00Z' }),
      makeSession({ id: 'p2', status: 'passed', started_at: '2026-04-18T12:00:00Z' }),
      makeSession({ id: 'f1', status: 'failed', started_at: '2026-04-17T12:00:00Z' }),
      makeSession({ id: 'e1', status: 'error',  started_at: '2026-04-16T12:00:00Z' }),
      makeSession({ id: 'old', status: 'passed', started_at: '2026-04-10T12:00:00Z' }),
    ];
    expect(passRate7d(sessions, now)).toBe(50);
  });

  it('failures7d counts failed + error in 7d', () => {
    const sessions = [
      makeSession({ id: 'p1', status: 'passed', started_at: '2026-04-19T12:00:00Z' }),
      makeSession({ id: 'f1', status: 'failed', started_at: '2026-04-18T12:00:00Z' }),
      makeSession({ id: 'e1', status: 'error',  started_at: '2026-04-17T12:00:00Z' }),
      makeSession({ id: 'old', status: 'failed', started_at: '2026-04-10T12:00:00Z' }),
    ];
    expect(failures7d(sessions, now)).toBe(2);
  });

  it('lastSession returns the most recent started_at', () => {
    const sessions = [
      makeSession({ id: 'a', started_at: '2026-04-18T12:00:00Z' }),
      makeSession({ id: 'b', started_at: '2026-04-20T09:00:00Z' }),
      makeSession({ id: 'c', started_at: '2026-04-19T12:00:00Z' }),
    ];
    expect(lastSession(sessions)).toBe('2026-04-20T09:00:00Z');
  });

  it('lastSession returns null for empty input', () => {
    expect(lastSession([])).toBeNull();
  });
});
