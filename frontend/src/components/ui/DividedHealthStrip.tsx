import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';

export type DividedHealthStripTone = 'ok' | 'warn' | 'error' | 'neutral';

export interface DividedHealthStripCell {
  icon: LucideIcon;
  label: string;
  tone: DividedHealthStripTone;
  value: ReactNode;
  detail?: ReactNode;
  to?: string;
  testId?: string;
}

interface DividedHealthStripProps {
  cells: DividedHealthStripCell[];
}

const TONE_TEXT: Record<DividedHealthStripTone, string> = {
  ok: 'text-success-foreground',
  warn: 'text-warning-foreground',
  error: 'text-danger-foreground',
  neutral: 'text-text-2',
};

const TONE_DOT: Record<DividedHealthStripTone, string> = {
  ok: 'bg-success-strong',
  warn: 'bg-warning-strong',
  error: 'bg-danger-strong',
  neutral: 'bg-neutral-strong',
};

export default function DividedHealthStrip({ cells }: DividedHealthStripProps) {
  return (
    <div className="flex flex-col divide-y divide-border rounded-lg border border-border md:flex-row md:divide-x md:divide-y-0">
      {cells.map((cell) => {
        const Icon = cell.icon;
        const content = (
          <>
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-text-2">
              <Icon size={14} className="text-text-3" />
              {cell.label}
            </div>
            <div className="mt-1.5 flex items-center gap-2">
              <span className={`inline-block h-2 w-2 rounded-full ${TONE_DOT[cell.tone]}`} />
              <span className={`text-sm font-medium ${TONE_TEXT[cell.tone]}`}>{cell.value}</span>
            </div>
            {cell.detail && <p className="mt-1 line-clamp-2 text-xs text-text-2">{cell.detail}</p>}
          </>
        );

        return (
          cell.to ? (
            <Link
              key={String(cell.label)}
              to={cell.to}
              data-testid={cell.testId}
              className="block flex-1 px-4 py-3 transition-colors hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-accent"
            >
              {content}
            </Link>
          ) : (
            <div key={String(cell.label)} data-testid={cell.testId} className="flex-1 px-4 py-3">
              {content}
            </div>
          )
        );
      })}
    </div>
  );
}
