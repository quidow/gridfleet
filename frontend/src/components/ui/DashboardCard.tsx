import type { ReactNode } from 'react';

type DashboardCardVariant = 'primary' | 'secondary';

interface DashboardCardProps {
  title: string;
  titleSlot?: ReactNode;
  description?: ReactNode;
  rightSlot?: ReactNode;
  variant?: DashboardCardVariant;
  footer?: ReactNode;
  children: ReactNode;
}

const CARD_CLASS: Record<DashboardCardVariant, string> = {
  primary: 'border-border bg-surface-1 shadow-sm',
  secondary: 'border-border bg-surface-1',
};

const HEADER_CLASS: Record<DashboardCardVariant, string> = {
  primary: 'bg-surface-1',
  secondary: 'bg-surface-soft',
};

export default function DashboardCard({
  title,
  titleSlot,
  description,
  rightSlot,
  variant = 'secondary',
  footer,
  children,
}: DashboardCardProps) {
  return (
    <section
      className={`section-gap overflow-hidden rounded-lg border ${CARD_CLASS[variant]}`}
      data-dashboard-card-variant={variant}
    >
      <div className={`flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between ${HEADER_CLASS[variant]}`}>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="heading-section">{title}</h2>
            {titleSlot}
          </div>
          {description ? <p className="mt-1 text-xs text-text-2">{description}</p> : null}
        </div>
        {rightSlot ? <div className="shrink-0">{rightSlot}</div> : null}
      </div>

      <div className="border-t border-border">{children}</div>

      {footer ? (
        <div className="border-t border-border bg-surface-soft px-5 py-4">
          {footer}
        </div>
      ) : null}
    </section>
  );
}
