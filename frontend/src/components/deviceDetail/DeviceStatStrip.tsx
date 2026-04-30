import { EMPTY_GLYPH, formatStat } from '../../utils/emptyValue';
import { formatRelativeTime } from '../../utils/dateFormatting';
import type { DeviceStatSummary } from './deviceStatStripSummary';

type Props = {
  summary: DeviceStatSummary;
  isLoading?: boolean;
  now?: Date;
};

type Card = { label: string; value: string };

function formatLastSession(value: string | null, now: Date): string {
  if (value === null) return EMPTY_GLYPH;
  return formatRelativeTime(value, now.getTime());
}

function buildCards(summary: DeviceStatSummary, now: Date): Card[] {
  return [
    { label: 'Sessions 24h', value: formatStat(summary.sessions24h) },
    { label: 'Pass rate 7d', value: formatStat(summary.passRate7d, { suffix: '%' }) },
    { label: 'Failures 7d', value: formatStat(summary.failures7d) },
    { label: 'Last session', value: formatLastSession(summary.lastSession, now) },
  ];
}

function StatSkeleton() {
  return (
    <div
      data-testid="device-stat-skeleton"
      className="h-16 animate-pulse rounded-lg border border-border bg-surface-1"
    />
  );
}

export default function DeviceStatStrip({
  summary,
  isLoading = false,
  now = new Date(),
}: Props) {
  if (isLoading) {
    return (
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatSkeleton />
        <StatSkeleton />
        <StatSkeleton />
        <StatSkeleton />
      </div>
    );
  }
  const cards = buildCards(summary, now);
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {cards.map((card) => (
        <div
          key={card.label}
          className="rounded-lg border border-border bg-surface-1 px-4 py-3 shadow-sm"
        >
          <p className="heading-label">{card.label}</p>
          <p className="metric-numeric mt-1 text-2xl font-semibold text-text-1">{card.value}</p>
        </div>
      ))}
    </div>
  );
}
