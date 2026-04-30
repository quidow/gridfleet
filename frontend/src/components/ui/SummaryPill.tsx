import type { ReactNode } from 'react';

export type SummaryPillTone = 'ok' | 'warn' | 'error' | 'neutral';

interface SummaryPillProps {
  tone: SummaryPillTone;
  label: string;
  value?: ReactNode;
}

const TONE_DOT: Record<SummaryPillTone, string> = {
  ok: 'bg-success-strong',
  warn: 'bg-warning-strong',
  error: 'bg-danger-strong',
  neutral: 'bg-neutral-strong',
};

export default function SummaryPill({ tone, label, value }: SummaryPillProps) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-1 px-2.5 py-1 text-xs text-text-2">
      <span className={`inline-block h-2 w-2 rounded-full ${TONE_DOT[tone]}`} />
      <span className="font-medium text-text-2">{label}</span>
      {value !== undefined && <span className="metric-numeric font-mono tabular-nums text-text-1">{value}</span>}
    </span>
  );
}
