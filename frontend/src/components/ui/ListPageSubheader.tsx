import { useId, type ReactNode } from 'react';

interface ListPageSubheaderProps {
  title: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  className?: string;
}

export default function ListPageSubheader({ title, meta, action, className = '' }: ListPageSubheaderProps) {
  const titleId = useId();

  return (
    <section
      aria-labelledby={titleId}
      data-testid="list-page-subheader"
      className={['mb-2 flex flex-wrap items-center gap-x-3 gap-y-1', className].filter(Boolean).join(' ')}
    >
      <h2 id={titleId} className="heading-subsection">
        {title}
      </h2>
      {meta ? <p className="text-xs text-text-2">{meta}</p> : null}
      {action ? <div className="ml-auto">{action}</div> : null}
    </section>
  );
}
