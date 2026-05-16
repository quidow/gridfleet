import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
  BarChart, Bar,
} from 'recharts';
import { Download } from 'lucide-react';
import { useSessionSummary } from '../../hooks/useAnalytics';
import { downloadAnalyticsCsv } from '../../api/analytics';
import { LoadingSpinner } from '../LoadingSpinner';
import { PlatformIcon } from '../PlatformIcon';
import AnalyticsEmptyState from './AnalyticsEmptyState';
import Card from '../ui/Card';
import { resolvePlatformLabel } from '../../lib/labels';
import type { AnalyticsParams } from '../../api/analytics';
import { formatDateOnly } from '../../utils/dateFormatting';

interface Props {
  params: AnalyticsParams;
}

function formatDuration(sec: number | null): string {
  if (sec === null) return '-';
  if (sec < 60) return `${Math.round(sec)}s`;
  return `${Math.round(sec / 60)}m`;
}

export default function SessionTrendsTab({ params }: Props) {
  const { data: byDay, isLoading: dayLoading } = useSessionSummary({ ...params, group_by: 'day' });
  const { data: byPlatform, isLoading: platLoading } = useSessionSummary({ ...params, group_by: 'platform' });

  if (dayLoading || platLoading) return <LoadingSpinner />;

  const dayData = (byDay ?? []).map((r) => ({
    date: formatDateOnly(r.group_key),
    passed: r.passed,
    failed: r.failed,
    error: r.error,
  }));

  return (
    <div className="space-y-8">
      {/* Sessions per day */}
      <Card padding="md">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-medium text-text-2">Sessions per Day</h3>
          <button
            onClick={() => downloadAnalyticsCsv('sessions/summary', { ...params, group_by: 'day' })}
            className="flex items-center gap-1 text-xs text-text-3 hover:text-text-2"
          >
            <Download size={14} /> CSV
          </button>
        </div>
        {dayData.length === 0 ? (
          <AnalyticsEmptyState
            title="No sessions in this period"
            description="Try expanding the date range or switching to a busier time window to see daily session activity."
          />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={dayData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 12 }} />
              <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
              <Tooltip />
              <Legend />
              <Line type="monotone" dataKey="passed" stroke="#22c55e" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="failed" stroke="#ef4444" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="error" stroke="#f59e0b" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Sessions by platform */}
      <Card padding="md">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-medium text-text-2">Sessions by Platform</h3>
          <button
            onClick={() => downloadAnalyticsCsv('sessions/summary', { ...params, group_by: 'platform' })}
            className="flex items-center gap-1 text-xs text-text-3 hover:text-text-2"
          >
            <Download size={14} /> CSV
          </button>
        </div>
        {(byPlatform ?? []).length === 0 ? (
          <AnalyticsEmptyState
            title="No platform breakdown yet"
            description="There were no matching sessions to group by platform for the selected window."
          />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={byPlatform}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="group_key" tick={{ fontSize: 12 }} tickFormatter={(v: string) => resolvePlatformLabel(v, null)} />
              <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
              <Tooltip />
              <Legend />
              <Bar dataKey="passed" stackId="status" fill="#22c55e" />
              <Bar dataKey="failed" stackId="status" fill="#ef4444" />
              <Bar dataKey="error" stackId="status" fill="#f59e0b" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Avg duration by platform table */}
      <Card padding="none">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h3 className="text-sm font-medium text-text-2">Average Session Duration by Platform</h3>
          <button
            onClick={() => downloadAnalyticsCsv('sessions/summary', { ...params, group_by: 'platform' })}
            className="flex items-center gap-1 text-xs text-text-3 hover:text-text-2"
          >
            <Download size={14} /> CSV
          </button>
        </div>
        {(byPlatform ?? []).length === 0 ? (
          <div className="px-5 py-6">
            <AnalyticsEmptyState
              title="No average duration data"
              description="Average session duration appears here once the selected range includes at least one completed session."
            />
          </div>
        ) : (
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Platform</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Sessions</th>
                <th className="px-5 py-3 text-left text-xs font-medium text-text-3 uppercase">Avg Duration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(byPlatform ?? []).map((row) => (
                <tr key={row.group_key} className="hover:bg-surface-2">
                  <td className="px-5 py-3 text-sm text-text-1"><PlatformIcon platformId={row.group_key} /></td>
                  <td className="px-5 py-3 text-sm text-text-2">{row.total}</td>
                  <td className="px-5 py-3 text-sm text-text-2">{formatDuration(row.avg_duration_sec)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
