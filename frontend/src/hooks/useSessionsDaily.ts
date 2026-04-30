import { useMemo } from 'react';
import { useSessionSummary } from './useAnalytics';
import type { SessionSummaryRow } from '../types/analytics';

interface DailySessionPoint {
  date: string; // YYYY-MM-DD
  total: number;
  passed: number;
  failed: number;
}

function toIsoDate(d: Date): string {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, '0');
  const day = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function normalizeKey(key: string): string | null {
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(key);
  return match ? match[1] : null;
}

export function buildDailySeries(rows: SessionSummaryRow[], today: Date, days: number): DailySessionPoint[] {
  const byDate = new Map<string, SessionSummaryRow>();
  for (const row of rows) {
    const normalized = normalizeKey(row.group_key);
    if (normalized) byDate.set(normalized, row);
  }

  const series: DailySessionPoint[] = [];
  for (let i = days - 1; i >= 0; i -= 1) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    const iso = toIsoDate(d);
    const row = byDate.get(iso);
    series.push({
      date: iso,
      total: row?.total ?? 0,
      passed: row?.passed ?? 0,
      failed: row?.failed ?? 0,
    });
  }
  return series;
}

export function useSessionsDaily(days = 7) {
  const dateRange = useMemo(() => {
    const to = new Date();
    const from = new Date(to);
    from.setUTCDate(from.getUTCDate() - (days - 1));
    from.setUTCHours(0, 0, 0, 0);
    return { date_from: from.toISOString(), date_to: to.toISOString() };
  }, [days]);

  const query = useSessionSummary({ ...dateRange, group_by: 'day' });

  const series = useMemo(() => {
    if (!query.data) return [] as DailySessionPoint[];
    const to = new Date(dateRange.date_to);
    return buildDailySeries(query.data, to, days);
  }, [query.data, days, dateRange.date_to]);

  return { ...query, series };
}
