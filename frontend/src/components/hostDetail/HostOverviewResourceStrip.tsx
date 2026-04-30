import { useHostResourceTelemetry } from '../../hooks/useHosts';
import type { HostResourceSample } from '../../types';

type Props = {
  hostId: string;
};

function pickLatestSample(samples: HostResourceSample[]): HostResourceSample | null {
  if (samples.length === 0) return null;
  return samples[samples.length - 1] ?? null;
}

function memoryPercent(sample: HostResourceSample | null): number | null {
  if (!sample || sample.memory_used_mb === null || sample.memory_total_mb === null) return null;
  if (sample.memory_total_mb <= 0) return null;
  return (sample.memory_used_mb / sample.memory_total_mb) * 100;
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(0)}%`;
}

function toneFor(percent: number | null): string {
  if (percent === null) return 'bg-surface-2';
  if (percent >= 90) return 'bg-danger-strong';
  if (percent >= 75) return 'bg-warning-strong';
  return 'bg-success-strong';
}

function Gauge({ label, percent }: { label: string; percent: number | null }) {
  const width = percent === null ? 0 : Math.min(100, Math.max(0, percent));
  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-text-3">{label}</span>
        <span className="text-sm font-semibold tabular-nums text-text-1">{formatPercent(percent)}</span>
      </div>
      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
        <div
          className={`h-full ${toneFor(percent)}`}
          style={{ width: `${width}%` }}
          aria-hidden="true"
        />
      </div>
    </div>
  );
}

export default function HostOverviewResourceStrip({ hostId }: Props) {
  const { data } = useHostResourceTelemetry(hostId);
  const latest = data ? pickLatestSample(data.samples) : null;

  return (
    <div
      className="rounded-lg border border-border bg-surface-1 p-5"
      aria-label="Host resource usage"
    >
      <h2 className="mb-3 text-sm font-medium text-text-3">Resource Usage</h2>
      <div className="flex flex-col gap-4 sm:flex-row sm:gap-6">
        <Gauge label="CPU" percent={latest?.cpu_percent ?? null} />
        <Gauge label="Memory" percent={memoryPercent(latest)} />
        <Gauge label="Disk" percent={latest?.disk_percent ?? null} />
      </div>
      {!latest ? (
        <p className="mt-3 text-xs text-text-3">
          No telemetry samples yet. See Diagnostics tab for history.
        </p>
      ) : null}
    </div>
  );
}
