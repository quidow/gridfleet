import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { LoadingSpinner } from '../LoadingSpinner';
import FetchError from '../ui/FetchError';
import AnalyticsEmptyState from '../analytics/AnalyticsEmptyState';
import { HardwareTelemetryStateBadge } from '../HardwareTelemetryStateBadge';
import { useHostResourceTelemetry } from '../../hooks/useHosts';
import { deriveHostResourceTelemetryState } from '../../lib/hostResourceTelemetry';
import { formatDateTime, formatRelativeTime } from '../../utils/dateFormatting';

type Props = {
  hostId: string;
  hostOnline: boolean;
};

type ChartPoint = {
  timestamp: string;
  cpuPercent: number | null;
  memoryPercent: number | null;
  diskPercent: number | null;
};

const TIME_TICK_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

function toChartPoints(
  samples: {
    timestamp: string;
    cpu_percent: number | null;
    memory_used_mb: number | null;
    memory_total_mb: number | null;
    disk_percent: number | null;
  }[],
): ChartPoint[] {
  return samples.map((sample) => ({
    timestamp: sample.timestamp,
    cpuPercent: sample.cpu_percent,
    memoryPercent:
      sample.memory_used_mb !== null &&
      sample.memory_total_mb !== null &&
      sample.memory_total_mb > 0
        ? (sample.memory_used_mb / sample.memory_total_mb) * 100
        : null,
    diskPercent: sample.disk_percent,
  }));
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(1)}%`;
}

function formatTick(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : TIME_TICK_FORMATTER.format(date);
}

function MetricCard({
  title,
  dataKey,
  stroke,
  points,
}: {
  title: string;
  dataKey: 'cpuPercent' | 'memoryPercent' | 'diskPercent';
  stroke: string;
  points: ChartPoint[];
}) {
  return (
    <div className="rounded-lg border border-border bg-surface-1 p-5">
      <h3 className="mb-4 text-sm font-medium text-text-2">{title}</h3>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={points}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="timestamp" tick={{ fontSize: 12 }} tickFormatter={formatTick} minTickGap={24} />
          <YAxis domain={[0, 100]} tick={{ fontSize: 12 }} tickFormatter={(value) => `${value}%`} width={44} />
          <Tooltip
            labelFormatter={(label) => formatDateTime(label)}
            formatter={(value) => formatPercent(typeof value === 'number' ? value : null)}
          />
          <Line
            type="monotone"
            dataKey={dataKey}
            stroke={stroke}
            strokeWidth={2}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function HostResourceTelemetryPanel({ hostId, hostOnline }: Props) {
  const { data, isLoading, error, refetch } = useHostResourceTelemetry(hostId);
  const telemetryState = deriveHostResourceTelemetryState(data?.latest_recorded_at ?? null, 60);
  const chartPoints = toChartPoints(data?.samples ?? []);
  const lastSampleText = data?.latest_recorded_at
    ? `Last sample ${formatRelativeTime(data.latest_recorded_at)} (${formatDateTime(data.latest_recorded_at)})`
    : 'No telemetry samples recorded yet.';

  return (
    <div className="rounded-lg border border-border bg-surface-1 p-5">
      <div className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-sm font-medium text-text-2">Host Resource Telemetry</h2>
          <p className="mt-1 text-sm text-text-3">
            Recent CPU, memory, and disk pressure sampled by the host agent and bucketed by the backend.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <HardwareTelemetryStateBadge state={telemetryState} />
          {!hostOnline ? (
            <span className="text-xs text-text-3">Host offline; charts show the last recorded samples.</span>
          ) : null}
        </div>
      </div>

      <p className="mt-3 text-sm text-text-3">{lastSampleText}</p>

      {isLoading ? (
        <LoadingSpinner />
      ) : error ? (
        <div className="mt-4">
          <FetchError
            message="Could not load host resource telemetry."
            onRetry={() => {
              void refetch();
            }}
          />
        </div>
      ) : !data || data.samples.length === 0 ? (
        <div className="mt-4">
          <AnalyticsEmptyState
            title="No telemetry samples in this window"
            description="Wait for a fresh sample or expand the telemetry window to inspect earlier host activity."
          />
        </div>
      ) : (
        <div className="mt-5 grid grid-cols-1 gap-5 xl:grid-cols-3">
          <MetricCard title="CPU" dataKey="cpuPercent" stroke="#2563eb" points={chartPoints} />
          <MetricCard title="Memory" dataKey="memoryPercent" stroke="#16a34a" points={chartPoints} />
          <MetricCard title="Disk" dataKey="diskPercent" stroke="#f59e0b" points={chartPoints} />
        </div>
      )}
    </div>
  );
}
