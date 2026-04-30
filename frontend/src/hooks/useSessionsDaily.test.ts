import { describe, it, expect } from 'vitest';
import { buildDailySeries } from './useSessionsDaily';

describe('buildDailySeries', () => {
  it('zero-fills missing days and returns series in ascending date order', () => {
    // Reference "today" = 2026-04-18
    const today = new Date('2026-04-18T12:00:00Z');
    const rows = [
      { group_key: '2026-04-14', total: 3, passed: 2, failed: 1, error: 0, avg_duration_sec: null },
      { group_key: '2026-04-17', total: 5, passed: 5, failed: 0, error: 0, avg_duration_sec: null },
    ];
    const series = buildDailySeries(rows, today, 7);
    expect(series.length).toBe(7);
    // dates: 12, 13, 14, 15, 16, 17, 18
    expect(series[0]).toEqual({ date: '2026-04-12', total: 0, passed: 0, failed: 0 });
    expect(series[2]).toEqual({ date: '2026-04-14', total: 3, passed: 2, failed: 1 });
    expect(series[5]).toEqual({ date: '2026-04-17', total: 5, passed: 5, failed: 0 });
    expect(series[6]).toEqual({ date: '2026-04-18', total: 0, passed: 0, failed: 0 });
  });

  it('ignores unrecognised group_keys', () => {
    const today = new Date('2026-04-18T00:00:00Z');
    const rows = [{ group_key: 'not-a-date', total: 99, passed: 99, failed: 0, error: 0, avg_duration_sec: null }];
    const series = buildDailySeries(rows, today, 3);
    expect(series.every((d) => d.total === 0)).toBe(true);
  });

  it('uses provided today snapshot, not the wall clock', () => {
    // Deliberately anchor to a date in the past.
    const today = new Date('2024-01-01T00:00:00Z');
    const rows = [{ group_key: '2023-12-31', total: 4, passed: 4, failed: 0, error: 0, avg_duration_sec: null }];
    const series = buildDailySeries(rows, today, 2);
    expect(series).toEqual([
      { date: '2023-12-31', total: 4, passed: 4, failed: 0 },
      { date: '2024-01-01', total: 0, passed: 0, failed: 0 },
    ]);
  });
});
