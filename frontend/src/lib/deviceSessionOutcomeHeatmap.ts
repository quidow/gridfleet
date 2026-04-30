import type { SessionOutcomeHeatmapRow } from '../types';

export type SessionOutcomeHeatmapSeverity = 'none' | 'passed' | 'failed' | 'error';

export type SessionOutcomeHeatmapCell = {
  dateKey: string;
  label: string;
  total: number;
  passed: number;
  failed: number;
  error: number;
  passRate: number | null;
  severity: SessionOutcomeHeatmapSeverity;
  inRange: boolean;
  isToday: boolean;
  title: string;
};

type SessionOutcomeHeatmapWeek = {
  id: string;
  monthLabel: string | null;
  cells: SessionOutcomeHeatmapCell[];
};

type SessionOutcomeHeatmapModel = {
  weeks: SessionOutcomeHeatmapWeek[];
  activeDays: number;
  totalSessions: number;
  passed: number;
  failed: number;
  error: number;
  passRate: number | null;
  hasData: boolean;
};

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const WEEKDAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;

type AggregateDay = {
  total: number;
  passed: number;
  failed: number;
  error: number;
  severity: SessionOutcomeHeatmapSeverity;
};

function getDateKeyFormatter(timeZone?: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}

function normalizeDate(value?: Date | string | number): Date {
  if (value instanceof Date) {
    return value;
  }
  if (typeof value === 'string' || typeof value === 'number') {
    return new Date(value);
  }
  return new Date();
}

function toDateKeyParts(dateKey: string): [number, number, number] {
  const [year, month, day] = dateKey.split('-').map(Number);
  return [year, month, day];
}

function formatDateKey(date: Date, timeZone?: string): string {
  const parts = getDateKeyFormatter(timeZone).formatToParts(date);
  const year = parts.find((part) => part.type === 'year')?.value;
  const month = parts.find((part) => part.type === 'month')?.value;
  const day = parts.find((part) => part.type === 'day')?.value;
  if (!year || !month || !day) {
    throw new Error('Unable to derive local date key');
  }
  return `${year}-${month}-${day}`;
}

function addDays(dateKey: string, days: number): string {
  const [year, month, day] = toDateKeyParts(dateKey);
  const shifted = new Date(Date.UTC(year, month - 1, day + days));
  return `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, '0')}-${String(shifted.getUTCDate()).padStart(2, '0')}`;
}

function getWeekday(dateKey: string): number {
  const [year, month, day] = toDateKeyParts(dateKey);
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

function monthLabel(dateKey: string): string {
  const [, month] = toDateKeyParts(dateKey);
  return MONTH_LABELS[month - 1];
}

function formatDateLabel(dateKey: string): string {
  const [year, month, day] = toDateKeyParts(dateKey);
  return `${String(day).padStart(2, '0')} ${MONTH_LABELS[month - 1]} ${year}`;
}

function severityForDay(day: AggregateDay): SessionOutcomeHeatmapSeverity {
  if (day.error > 0) return 'error';
  if (day.failed > 0) return 'failed';
  if (day.passed > 0) return 'passed';
  return 'none';
}

function buildTitle(dateKey: string, day: AggregateDay): string {
  if (day.total === 0) {
    return `${formatDateLabel(dateKey)}: no completed sessions`;
  }

  const passRate = Math.round((day.passed / day.total) * 100);
  const parts = [`${formatDateLabel(dateKey)}: ${day.total} sessions`];
  parts.push(`${day.passed} passed`);
  parts.push(`${day.failed} failed`);
  parts.push(`${day.error} error`);
  parts.push(`${passRate}% pass rate`);
  return parts.join(' · ');
}

export function buildSessionOutcomeHeatmap(
  rows: SessionOutcomeHeatmapRow[],
  days = 90,
  options: { now?: Date | string | number; timeZone?: string } = {},
): SessionOutcomeHeatmapModel {
  const todayKey = formatDateKey(normalizeDate(options.now), options.timeZone);
  const startKey = addDays(todayKey, -(days - 1));
  const paddedStart = addDays(startKey, -getWeekday(startKey));
  const paddedEnd = addDays(todayKey, 6 - getWeekday(todayKey));
  const aggregates = new Map<string, AggregateDay>();

  for (let cursor = startKey; cursor <= todayKey; cursor = addDays(cursor, 1)) {
    aggregates.set(cursor, { total: 0, passed: 0, failed: 0, error: 0, severity: 'none' });
  }

  for (const row of rows) {
    const dateKey = formatDateKey(new Date(row.timestamp), options.timeZone);
    const aggregate = aggregates.get(dateKey);
    if (!aggregate) {
      continue;
    }
    aggregate.total += 1;
    aggregate[row.status] += 1;
    aggregate.severity = severityForDay(aggregate);
  }

  const totals = [...aggregates.values()].reduce(
    (summary, day) => {
      summary.totalSessions += day.total;
      summary.passed += day.passed;
      summary.failed += day.failed;
      summary.error += day.error;
      if (day.total > 0) {
        summary.activeDays += 1;
      }
      return summary;
    },
    { totalSessions: 0, passed: 0, failed: 0, error: 0, activeDays: 0 },
  );

  const weeks: SessionOutcomeHeatmapWeek[] = [];
  let previousMonth: string | null = null;
  let weekIndex = 0;
  for (let cursor = paddedStart; cursor <= paddedEnd;) {
    const cells: SessionOutcomeHeatmapCell[] = [];
    let currentMonthLabel: string | null = null;
    for (let dayIndex = 0; dayIndex < WEEKDAY_LABELS.length; dayIndex += 1) {
      const aggregate = aggregates.get(cursor) ?? {
        total: 0,
        passed: 0,
        failed: 0,
        error: 0,
        severity: 'none' as const,
      };
      const inRange = cursor >= startKey && cursor <= todayKey;
      const label = formatDateLabel(cursor);
      const cell: SessionOutcomeHeatmapCell = {
        dateKey: cursor,
        label,
        total: aggregate.total,
        passed: aggregate.passed,
        failed: aggregate.failed,
        error: aggregate.error,
        passRate: aggregate.total > 0 ? Math.round((aggregate.passed / aggregate.total) * 100) : null,
        severity: aggregate.severity,
        inRange,
        isToday: cursor === todayKey,
        title: buildTitle(cursor, aggregate),
      };
      if (inRange && currentMonthLabel === null) {
        const nextMonth = monthLabel(cursor);
        if (nextMonth !== previousMonth) {
          currentMonthLabel = nextMonth;
          previousMonth = nextMonth;
        }
      }
      cells.push(cell);
      cursor = addDays(cursor, 1);
    }

    weeks.push({
      id: `week-${weekIndex}`,
      monthLabel: currentMonthLabel,
      cells,
    });
    weekIndex += 1;
  }

  return {
    weeks,
    activeDays: totals.activeDays,
    totalSessions: totals.totalSessions,
    passed: totals.passed,
    failed: totals.failed,
    error: totals.error,
    passRate: totals.totalSessions > 0 ? Math.round((totals.passed / totals.totalSessions) * 100) : null,
    hasData: totals.totalSessions > 0,
  };
}

export const SESSION_OUTCOME_HEATMAP_WEEKDAY_LABELS = WEEKDAY_LABELS;
