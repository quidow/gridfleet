import { isValidElement, useEffect, useState, type ReactNode } from 'react';

interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  updatedAt?: Date | string | number | null;
  summary?: ReactNode;
  actions?: ReactNode;
}

function toTimestamp(value: Date | string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (value instanceof Date) {
    const timestamp = value.getTime();
    return Number.isFinite(timestamp) ? timestamp : null;
  }
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatUpdatedAt(
  value: Date | string | number | null | undefined,
  now: number,
): string | null {
  const timestamp = toTimestamp(value);
  if (timestamp === null) return null;

  const diff = Math.max(0, Math.floor((now - timestamp) / 1000));
  if (diff < 10) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d ago`;
  return `${Math.floor(diff / (7 * 86400))}w ago`;
}

function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

function renderDescription(subtitle: ReactNode, updatedLabel: string | null): ReactNode {
  if (typeof subtitle === 'string' && subtitle.length > 0) {
    return updatedLabel ? `${subtitle} · updated ${updatedLabel}` : subtitle;
  }

  if (isValidElement(subtitle)) {
    return updatedLabel ? (
      <>
        {subtitle}
        <span> · updated {updatedLabel}</span>
      </>
    ) : (
      subtitle
    );
  }

  return updatedLabel ? `Updated ${updatedLabel}` : null;
}

export default function PageHeader({ title, subtitle, updatedAt, summary, actions }: PageHeaderProps) {
  const now = useNow();
  const updatedLabel = formatUpdatedAt(updatedAt, now);
  const description = renderDescription(subtitle, updatedLabel);

  return (
    <header className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h1 className="heading-page">{title}</h1>
        {description && <p className="mt-1 text-xs text-text-2">{description}</p>}
      </div>
      {(summary || actions) && (
        <div className="flex flex-col items-start gap-2 sm:items-end">
          {summary && <div className="flex flex-wrap items-center gap-2">{summary}</div>}
          {actions}
        </div>
      )}
    </header>
  );
}
