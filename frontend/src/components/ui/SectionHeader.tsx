import type { ReactNode } from 'react';

interface SectionHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  level?: 1 | 2 | 3;
  className?: string;
}

export default function SectionHeader({
  title,
  description,
  actions,
  level = 2,
  className = '',
}: SectionHeaderProps) {
  const Tag = `h${level}` as 'h1' | 'h2' | 'h3';
  const headingClass =
    level === 1
      ? 'heading-page'
      : level === 2
        ? 'heading-section'
        : 'heading-subsection';

  return (
    <div className={['flex items-start justify-between gap-4', className].filter(Boolean).join(' ')}>
      <div>
        <Tag className={headingClass}>{title}</Tag>
        {description && <p className="mt-1 text-sm text-text-3">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
