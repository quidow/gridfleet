import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import AnalyticsEmptyState from './AnalyticsEmptyState';
import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';
import type { AnalyticsParams } from '../../api/analytics';
import type { FleetCapacityTimelinePoint } from '../../types';
import { buildFleetCapacityChartData } from '../../lib/fleetCapacityTimeline';
import SectionSkeleton from '../ui/SectionSkeleton';
import FetchError from '../ui/FetchError';

interface Props {
  params: AnalyticsParams;
}

type TooltipPayloadItem = {
  name?: string;
  value?: number | string | null;
  color?: string;
};

interface CapacityTooltipProps {
  active?: boolean;
  label?: string;
  payload?: TooltipPayloadItem[];
}

function maxMetric(rows: FleetCapacityTimelinePoint[], key: keyof FleetCapacityTimelinePoint): number {
  return rows.reduce((max, row) => {
    const value = row[key];
    return typeof value === 'number' ? Math.max(max, value) : max;
  }, 0);
}

function CapacityTooltip({ active, label, payload }: CapacityTooltipProps) {
  if (!active || !payload?.length) return null;
  const visibleRows = payload.filter((item) => item.value !== null && item.value !== undefined);

  return (
    <div className="rounded-md border border-border bg-surface-1 px-3 py-2 shadow-lg">
      <p className="text-xs font-medium text-text-1">{label}</p>
      <div className="mt-2 space-y-1">
        {visibleRows.map((item) => (
          <div key={item.name} className="flex items-center gap-2 text-xs text-text-2">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: item.color }} />
            <span>{item.name}</span>
            <span className="font-mono tabular-nums text-text-1">{item.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function MetricSummary({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface-1 px-4 py-3">
      <p className="text-xs font-medium uppercase text-text-3">{label}</p>
      <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-text-1">{value}</p>
      <p className="mt-1 text-xs text-text-3">{detail}</p>
    </div>
  );
}

export default function FleetCapacityTab({ params }: Props) {
  const queryParams = { ...params, bucket_minutes: 1 };
  const { data, isLoading, isError, refetch } = useFleetCapacityTimeline(queryParams);

  if (isLoading) return <SectionSkeleton shape="split" rows={3} label="Fleet capacity loading" />;
  if (isError) {
    return <FetchError message="Could not load fleet capacity timeline." onRetry={() => void refetch()} />;
  }

  const rows = data?.series ?? [];
  const chartData = buildFleetCapacityChartData(data);
  const hasGaps = chartData.some((row) => row.isGap);

  if (rows.length === 0) {
    return (
      <AnalyticsEmptyState
        title="No capacity snapshots yet"
        description="Fleet supply, active sessions, queued requests, and unfulfilled attempts will appear after the collector records snapshots."
      />
    );
  }

  const latest = rows[rows.length - 1];
  const rejectedTotal = rows.reduce((sum, row) => sum + row.rejected_unfulfilled_sessions, 0);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-sm font-medium text-text-2">Fleet Capacity</h3>
        <p className="mt-1 text-sm text-text-3">
          Demand includes active sessions, queued Grid requests, and capacity-related session failures.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <MetricSummary label="Supply" value={latest.total_capacity_slots} detail="Latest schedulable slots" />
        <MetricSummary label="Active usage" value={latest.active_sessions} detail="Latest active sessions" />
        <MetricSummary label="Queued requests" value={maxMetric(rows, 'queued_requests')} detail="Peak queued Grid work" />
        <MetricSummary label="Peak inferred demand" value={maxMetric(rows, 'inferred_demand')} detail={`${rejectedTotal} unfulfilled attempts`} />
      </div>

      <div className="rounded-lg border border-border bg-surface-1 p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-medium text-text-2">Demand vs. Supply</h4>
            <p className="mt-1 text-xs text-text-3">
              Unfulfilled attempts are capacity or matching failures recorded before real execution.
            </p>
          </div>
          {hasGaps && (
            <p className="rounded-md border border-warning-soft bg-warning-soft px-3 py-1 text-xs text-warning-foreground">
              Collection gaps detected. Lines break where snapshots are missing.
            </p>
          )}
        </div>

        <ResponsiveContainer width="100%" height={340}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" minTickGap={24} tick={{ fontSize: 12 }} />
            <YAxis allowDecimals={false} tick={{ fontSize: 12 }} />
            <Tooltip content={<CapacityTooltip />} />
            <Legend />
            <Line
              type="linear"
              dataKey="total_capacity_slots"
              name="Supply"
              stroke="#2563eb"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
            />
            <Line
              type="linear"
              dataKey="active_sessions"
              name="Active usage"
              stroke="#16a34a"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
            />
            <Line
              type="linear"
              dataKey="queued_requests"
              name="Queued requests"
              stroke="#d97706"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
            />
            <Line
              type="linear"
              dataKey="rejected_unfulfilled_sessions"
              name="Unfulfilled attempts"
              stroke="#dc2626"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
            />
            <Line
              type="linear"
              dataKey="inferred_demand"
              name="Inferred demand"
              stroke="#7c3aed"
              strokeDasharray="5 4"
              strokeWidth={2}
              dot={false}
              connectNulls={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
