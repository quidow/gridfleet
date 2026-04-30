import { useMemo } from 'react';
import { useFleetCapacityTimeline } from '../../hooks/useAnalytics';

const BUCKET_MINUTES = 60;
const VIEW_W = 600;
const VIEW_H = 72;
const PAD_TOP = 6;
const PAD_BOT = 6;

type Tone = 'healthy' | 'warn' | 'critical';

interface LiveFleetHealthPoint {
  devices_total: number;
  devices_offline: number;
  devices_maintenance: number;
}

interface FleetHealthHistoryProps {
  livePoint?: LiveFleetHealthPoint;
}

function toneFor(pct: number): Tone {
  if (pct >= 80) return 'healthy';
  if (pct >= 50) return 'warn';
  return 'critical';
}

const TONE_STROKE: Record<Tone, string> = {
  healthy: 'text-success-strong',
  warn: 'text-warning-strong',
  critical: 'text-danger-strong',
};

const TONE_VALUE: Record<Tone, string> = {
  healthy: 'text-success-foreground',
  warn: 'text-warning-foreground',
  critical: 'text-danger-foreground',
};

function linePath(points: { x: number; y: number }[]): string {
  if (points.length === 0) return '';
  const [first, ...rest] = points;
  return [
    `M ${first!.x.toFixed(2)} ${first!.y.toFixed(2)}`,
    ...rest.map((point) => `L ${point.x.toFixed(2)} ${point.y.toFixed(2)}`),
  ].join(' ');
}

function reachablePct(point: LiveFleetHealthPoint): number {
  if (!point.devices_total) return 0;
  const reachable = Math.max(0, point.devices_total - point.devices_offline - point.devices_maintenance);
  return (reachable / point.devices_total) * 100;
}

export default function FleetHealthHistory({ livePoint }: FleetHealthHistoryProps) {
  const { data } = useFleetCapacityTimeline({ bucket_minutes: BUCKET_MINUTES });

  const chart = useMemo(() => {
    const series = data?.series ?? [];
    const percentages = series.map((p) => reachablePct(p));
    if (livePoint && livePoint.devices_total > 0) {
      percentages.push(reachablePct(livePoint));
    }

    if (percentages.length < 2) {
      return { hasData: false as const };
    }

    const usableH = VIEW_H - PAD_TOP - PAD_BOT;
    const stepX = VIEW_W / (percentages.length - 1);
    const points = percentages.map((v, i) => ({
      x: i * stepX,
      y: PAD_TOP + (1 - v / 100) * usableH,
    }));

    const tracePath = linePath(points);
    const areaPath = `${tracePath} L ${VIEW_W} ${VIEW_H} L 0 ${VIEW_H} Z`;

    const last = percentages[percentages.length - 1] ?? 0;
    const avg = percentages.reduce((a, b) => a + b, 0) / percentages.length;
    const lastPoint = points[points.length - 1]!;

    return {
      hasData: true as const,
      linePath: tracePath,
      areaPath,
      lastPct: last,
      avgPct: avg,
      lastX: lastPoint.x,
      lastY: lastPoint.y,
      midY: PAD_TOP + usableH * 0.5,
    };
  }, [data, livePoint]);

  if (!chart.hasData) {
    return (
      <div className="mt-4 flex flex-1 flex-col border-t border-border pt-4">
        <div className="flex items-baseline justify-between">
          <div>
            <p className="heading-label">Fleet health</p>
            <p className="mt-0.5 text-xs text-text-3">Last 24 hours</p>
          </div>
        </div>
        <p className="mt-3 text-xs text-text-2">Not enough history to plot.</p>
      </div>
    );
  }

  const tone = toneFor(chart.lastPct);
  const gradientId = 'fleet-health-area-gradient';

  return (
    <div className="mt-4 flex flex-1 flex-col border-t border-border pt-4">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <p className="heading-label">Fleet health</p>
          <p className="mt-0.5 text-xs text-text-3">Last 24 hours</p>
        </div>
        <div className="flex items-baseline gap-3 font-mono tabular-nums">
          <span className={`text-xl font-semibold ${TONE_VALUE[tone]}`}>
            {Math.round(chart.lastPct)}
            <span className="ml-0.5 text-xs font-normal text-text-2">%</span>
          </span>
          <span className="text-xs text-text-2">
            avg <span className="text-text-1">{Math.round(chart.avgPct)}%</span>
          </span>
        </div>
      </div>

      <div className={`relative mt-3 flex min-h-20 flex-1 ${TONE_STROKE[tone]}`}>
        <svg
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          className="block h-full w-full"
          preserveAspectRatio="none"
          role="img"
          aria-label={`Fleet health reachability over last 24 hours, currently ${Math.round(chart.lastPct)} percent, average ${Math.round(chart.avgPct)} percent`}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
              <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
            </linearGradient>
          </defs>

          <line
            x1="0"
            y1={PAD_TOP}
            x2={VIEW_W}
            y2={PAD_TOP}
            stroke="currentColor"
            strokeOpacity="0.18"
            strokeWidth="1"
            strokeDasharray="2 4"
            vectorEffect="non-scaling-stroke"
          />
          <line
            x1="0"
            y1={chart.midY}
            x2={VIEW_W}
            y2={chart.midY}
            stroke="currentColor"
            strokeOpacity="0.1"
            strokeWidth="1"
            strokeDasharray="2 4"
            vectorEffect="non-scaling-stroke"
          />

          <path d={chart.areaPath} fill={`url(#${gradientId})`} />
          <path
            d={chart.linePath}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
          <circle
            cx={chart.lastX}
            cy={chart.lastY}
            r="3"
            fill="currentColor"
            stroke="var(--color-surface-1, white)"
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
          />
        </svg>

        <span className="pointer-events-none absolute left-0 top-0 -translate-y-1 text-xs font-medium uppercase tracking-wide text-text-3 opacity-70">
          100%
        </span>
      </div>

      <div className="mt-2 flex justify-between text-xs font-medium uppercase tracking-wide text-text-3">
        <span>24h ago</span>
        <span>12h</span>
        <span>now</span>
      </div>
    </div>
  );
}
