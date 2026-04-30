import { describe, expect, it } from 'vitest';
import { buildSessionOutcomeHeatmap } from './deviceSessionOutcomeHeatmap';

describe('device session outcome heatmap', () => {
  it('buckets UTC timestamps into the requested local day', () => {
    const heatmap = buildSessionOutcomeHeatmap(
      [{ timestamp: '2026-04-16T01:30:00Z', status: 'passed' }],
      7,
      { now: '2026-04-16T12:00:00Z', timeZone: 'America/Los_Angeles' },
    );

    const targetCell = heatmap.weeks.flatMap((week) => week.cells).find((cell) => cell.dateKey === '2026-04-15');
    expect(targetCell).toMatchObject({
      total: 1,
      passed: 1,
      severity: 'passed',
    });
  });

  it('collapses multiple sessions into one daily cell and uses worst status', () => {
    const heatmap = buildSessionOutcomeHeatmap(
      [
        { timestamp: '2026-04-15T10:00:00Z', status: 'passed' },
        { timestamp: '2026-04-15T11:00:00Z', status: 'failed' },
        { timestamp: '2026-04-15T12:00:00Z', status: 'error' },
      ],
      7,
      { now: '2026-04-16T12:00:00Z', timeZone: 'UTC' },
    );

    const targetCell = heatmap.weeks.flatMap((week) => week.cells).find((cell) => cell.dateKey === '2026-04-15');
    expect(targetCell).toMatchObject({
      total: 3,
      passed: 1,
      failed: 1,
      error: 1,
      passRate: 33,
      severity: 'error',
    });
  });

  it('derives summary totals and pass rate across active days', () => {
    const heatmap = buildSessionOutcomeHeatmap(
      [
        { timestamp: '2026-04-14T10:00:00Z', status: 'passed' },
        { timestamp: '2026-04-15T10:00:00Z', status: 'passed' },
        { timestamp: '2026-04-15T11:00:00Z', status: 'failed' },
      ],
      7,
      { now: '2026-04-16T12:00:00Z', timeZone: 'UTC' },
    );

    expect(heatmap).toMatchObject({
      activeDays: 2,
      totalSessions: 3,
      passed: 2,
      failed: 1,
      error: 0,
      passRate: 67,
      hasData: true,
    });
  });

  it('pads empty ranges into full-week columns', () => {
    const heatmap = buildSessionOutcomeHeatmap([], 90, { now: '2026-04-16T12:00:00Z', timeZone: 'UTC' });

    expect(heatmap.hasData).toBe(false);
    expect(heatmap.weeks.length).toBeGreaterThanOrEqual(13);
    expect(heatmap.weeks.every((week) => week.cells.length === 7)).toBe(true);
  });
});
