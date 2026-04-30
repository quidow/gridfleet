import type { LucideIcon } from 'lucide-react';
import Sparkline from './Sparkline';

export type StatCardTone = 'neutral' | 'positive' | 'warn' | 'critical';

const TONE_BORDER: Record<StatCardTone, string> = {
  neutral: 'border-l-border',
  positive: 'border-l-success-strong',
  warn: 'border-l-warning-strong',
  critical: 'border-l-danger-strong',
};

const TONE_ICON: Record<StatCardTone, string> = {
  neutral: 'bg-neutral-soft text-neutral-foreground',
  positive: 'bg-success-soft text-success-foreground',
  warn: 'bg-warning-soft text-warning-foreground',
  critical: 'bg-danger-soft text-danger-foreground',
};

const TONE_SPARKLINE: Record<StatCardTone, string> = {
  neutral: 'text-text-3',
  positive: 'text-success-strong',
  warn: 'text-warning-strong',
  critical: 'text-danger-strong',
};

function hasVariation(values: number[] | undefined): values is number[] {
  if (!values || values.length < 2) return false;
  return Math.max(...values) - Math.min(...values) > 0;
}

export default function StatCard({
  label,
  value,
  icon: Icon,
  tone = 'neutral',
  hint,
  sparkline,
}: {
  label: string;
  value: number | string;
  icon: LucideIcon;
  tone?: StatCardTone;
  hint?: string;
  sparkline?: number[];
}) {
  return (
    <div data-testid="stat-card" className={`card card-padding hover-lift border-l-4 ${TONE_BORDER[tone]}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="heading-label">{label}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-text-1">{value}</p>
          {hint && <p className="mt-1 text-xs text-text-3">{hint}</p>}
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className={`rounded-lg p-2.5 ${TONE_ICON[tone]}`}>
            <Icon size={18} />
          </div>
          {hasVariation(sparkline) ? (
            <Sparkline
              values={sparkline}
              width={64}
              className={TONE_SPARKLINE[tone]}
              ariaLabel={`${label} trend`}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
