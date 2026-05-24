import { describe, expect, it } from 'vitest';
import { formatDuration, formatWaitTime } from './dateFormatting';

describe('formatDuration', () => {
  const start = '2026-04-20T10:00:00Z';

  it('returns seconds when diff < 1 minute', () => {
    expect(formatDuration(start, '2026-04-20T10:00:42Z')).toBe('42s');
  });

  it('returns m ss when diff < 1 hour', () => {
    expect(formatDuration(start, '2026-04-20T10:05:30Z')).toBe('5m 30s');
  });

  it('returns h mm when diff >= 1 hour', () => {
    expect(formatDuration(start, '2026-04-20T12:34:00Z')).toBe('2h 34m');
  });

  it('uses nowMs when end is null', () => {
    const now = new Date('2026-04-20T10:00:15Z').getTime();
    expect(formatDuration(start, null, now)).toBe('15s');
  });

  it('returns 0s for negative diff', () => {
    expect(formatDuration('2026-04-20T10:05:00Z', '2026-04-20T10:04:00Z')).toBe('0s');
  });

  it('returns "-" for invalid start', () => {
    expect(formatDuration('not-a-date', null)).toBe('-');
  });
});

describe('formatWaitTime', () => {
  it('returns "—" for undefined input', () => {
    expect(formatWaitTime(undefined)).toBe('—');
  });

  it('returns "—" for invalid ISO string', () => {
    expect(formatWaitTime('not-a-date')).toBe('—');
  });

  it('formats seconds when under 60s', () => {
    const now = Date.now();
    const thirtySecsAgo = new Date(now - 30_000).toISOString();
    expect(formatWaitTime(thirtySecsAgo, now)).toBe('30s');
  });

  it('formats minutes and seconds', () => {
    const now = Date.now();
    const twoMinAgo = new Date(now - 150_000).toISOString();
    expect(formatWaitTime(twoMinAgo, now)).toBe('2m 30s');
  });

  it('formats hours and minutes', () => {
    const now = Date.now();
    const oneHourFiveMin = new Date(now - 3_900_000).toISOString();
    expect(formatWaitTime(oneHourFiveMin, now)).toBe('1h 5m');
  });

  it('returns "0s" for future timestamps', () => {
    const now = Date.now();
    const future = new Date(now + 10_000).toISOString();
    expect(formatWaitTime(future, now)).toBe('0s');
  });
});
