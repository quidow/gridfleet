import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Link } from 'react-router-dom';

type AttentionListTone = 'neutral' | 'warn' | 'critical';

interface AttentionListRow {
  label: string;
  values: ReactNode;
  to?: string;
}

interface AttentionListCardProps {
  title: string;
  description?: string;
  total: number;
  tone: AttentionListTone;
  rows: AttentionListRow[];
}

const TONE_BORDER: Record<AttentionListTone, string> = {
  neutral: 'border-l-border',
  warn: 'border-l-warning-strong',
  critical: 'border-l-danger-strong',
};

const TONE_ICON: Record<AttentionListTone, string> = {
  neutral: 'bg-surface-2 text-text-3',
  warn: 'bg-warning-soft text-warning-foreground',
  critical: 'bg-danger-soft text-danger-foreground',
};

export default function AttentionListCard({
  title,
  description,
  total,
  tone,
  rows,
}: AttentionListCardProps) {
  if (rows.length === 0) return null;

  return (
    <div className={`card card-padding border-l-4 ${TONE_BORDER[tone]}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-text-3">{title}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-text-1">{total}</p>
          {description && <p className="mt-0.5 text-xs text-text-3">{description}</p>}
        </div>
        <div className={`rounded-lg p-2.5 ${TONE_ICON[tone]}`}>
          <AlertTriangle size={18} />
        </div>
      </div>

      <ul className="mt-3 divide-y divide-border text-sm">
        {rows.map((row, index) => (
          <li key={index} className="flex items-center justify-between py-2">
            {row.to ? (
              <Link to={row.to} className="text-text-2 hover:text-text-1">
                {row.label}
              </Link>
            ) : (
              <span className="text-text-2">{row.label}</span>
            )}
            <span className="text-xs tabular-nums text-text-3">{row.values}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
