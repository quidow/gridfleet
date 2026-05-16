import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
  PieChart, Pie,
} from 'recharts';
import { Download } from 'lucide-react';
import { useDeviceUtilization } from '../../hooks/useAnalytics';
import { downloadAnalyticsCsv } from '../../api/analytics';
import { LoadingSpinner } from '../LoadingSpinner';
import AnalyticsEmptyState from './AnalyticsEmptyState';
import Card from '../ui/Card';
import type { AnalyticsParams } from '../../api/analytics';

interface Props {
  params: AnalyticsParams;
}

function barColor(pct: number): string {
  if (pct > 90) return '#ef4444'; // red
  if (pct < 10) return '#f59e0b'; // amber
  return '#3b82f6'; // blue
}

const PIE_COLORS = ['#3b82f6', '#d1d5db', '#ef4444'];

export default function DeviceUtilizationTab({ params }: Props) {
  const { data, isLoading } = useDeviceUtilization(params);

  if (isLoading) return <LoadingSpinner />;

  const rows = data ?? [];
  const underutilized = rows.filter((d) => d.busy_pct < 10);
  const overloaded = rows.filter((d) => d.busy_pct > 90);

  // Fleet-wide pie data
  const totalBusy = rows.reduce((s, d) => s + d.total_session_time_sec, 0);
  const totalIdle = rows.reduce((s, d) => s + d.idle_time_sec, 0);
  const pieData = [
    { name: 'Busy', value: Math.round(totalBusy) },
    { name: 'Idle', value: Math.round(totalIdle) },
  ];

  return (
    <div className="space-y-8">
      {/* Per-device busy % */}
      <Card padding="md">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-medium text-text-2">Device Utilization (%)</h3>
          <button
            onClick={() => downloadAnalyticsCsv('devices/utilization', params)}
            className="flex items-center gap-1 text-xs text-text-3 hover:text-text-2"
          >
            <Download size={14} /> CSV
          </button>
        </div>
        {rows.length === 0 ? (
          <AnalyticsEmptyState
            title="No utilization data for this period"
            description="Try expanding the date range to include more session activity and idle time."
          />
        ) : (
          <ResponsiveContainer width="100%" height={Math.max(300, rows.length * 40)}>
            <BarChart data={rows} layout="vertical" margin={{ left: 120 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 12 }} unit="%" />
              <YAxis type="category" dataKey="device_name" tick={{ fontSize: 12 }} width={110} />
              <Tooltip formatter={(value) => `${Number(value).toFixed(1)}%`} />
              <Bar dataKey="busy_pct" name="Busy %">
                {rows.map((entry, idx) => (
                  <Cell key={idx} fill={barColor(entry.busy_pct)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </Card>

      {/* Highlight cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card padding="md">
          <h3 className="text-sm font-medium text-warning-foreground mb-3">
            Underutilized ({'<'}10%) — {underutilized.length}
          </h3>
          {underutilized.length === 0 ? (
            <p className="text-sm text-text-3">None</p>
          ) : (
            <ul className="space-y-1">
              {underutilized.map((d) => (
                <li key={d.device_id} className="text-sm text-text-2">
                  {d.device_name} <span className="text-text-3">({d.busy_pct.toFixed(1)}%)</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card padding="md">
          <h3 className="text-sm font-medium text-danger-foreground mb-3">
            Overloaded ({'>'}90%) — {overloaded.length}
          </h3>
          {overloaded.length === 0 ? (
            <p className="text-sm text-text-3">None</p>
          ) : (
            <ul className="space-y-1">
              {overloaded.map((d) => (
                <li key={d.device_id} className="text-sm text-text-2">
                  {d.device_name} <span className="text-text-3">({d.busy_pct.toFixed(1)}%)</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* Fleet pie chart */}
      <Card padding="md">
        <h3 className="text-sm font-medium text-text-2 mb-4">Fleet Time Breakdown</h3>
        {rows.length === 0 ? (
          <AnalyticsEmptyState
            title="No fleet time breakdown yet"
            description="Busy and idle time will appear here once devices have recorded activity in the selected range."
          />
        ) : (
          <div className="flex items-center justify-center">
            <ResponsiveContainer width={350} height={250}>
              <PieChart>
                <Pie
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={60}
                  outerRadius={100}
                  dataKey="value"
                  label={({ name, percent }: { name?: string; percent?: number }) => `${name ?? ''} ${((percent ?? 0) * 100).toFixed(0)}%`}
                >
                  {pieData.map((_entry, idx) => (
                    <Cell key={idx} fill={PIE_COLORS[idx]} />
                  ))}
                </Pie>
                <Tooltip formatter={(value) => `${Math.round(Number(value) / 3600)}h`} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}
