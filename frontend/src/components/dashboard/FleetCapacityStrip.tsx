import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';
import Sparkline from '../ui/Sparkline';

const BUCKET_MINUTES = 15;

type Slice = {
  key: string;
  label: string;
  className: string;
  values: number[];
};

export default function FleetCapacityStrip({ className }: { className?: string }) {
  const { data } = useFleetCapacityTimeline({ bucket_minutes: BUCKET_MINUTES });
  const series = (data?.series ?? []).filter((point) => point.has_data);
  if (series.length < 2) return null;

  const slices: Slice[] = [
    {
      key: 'devices_total',
      label: 'Total devices',
      className: 'text-text-3',
      values: series.map((p) => p.devices_total),
    },
    {
      key: 'devices_available',
      label: 'Available devices',
      className: 'text-success-strong',
      values: series.map((p) => p.devices_available),
    },
    {
      key: 'active_sessions',
      label: 'Active sessions',
      className: 'text-accent',
      values: series.map((p) => p.active_sessions),
    },
  ];

  return (
    <div className={`grid grid-cols-1 gap-3 sm:grid-cols-3 ${className ?? ''}`}>
      {slices.map((slice) => (
        <div key={slice.key} className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-2 px-3 py-2">
          <div className="min-w-0">
            <p className="heading-label">{slice.label}</p>
            <p className="metric-numeric mt-0.5 font-mono text-sm font-semibold tabular-nums text-text-1">
              {slice.values[slice.values.length - 1]}
            </p>
          </div>
          <Sparkline
            values={slice.values}
            width={80}
            height={28}
            className={slice.className}
            ariaLabel={`${slice.label} trend`}
          />
        </div>
      ))}
    </div>
  );
}
