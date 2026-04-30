import type { ReactNode } from 'react';

interface FilterBarProps {
  children: ReactNode;
  onClear?: () => void;
  trailing?: ReactNode;
  className?: string;
}

export default function FilterBar({ children, onClear, trailing, className = '' }: FilterBarProps) {
  return (
    <div
      className={[
        'flex flex-wrap items-center gap-3',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
      {onClear && (
        <button
          type="button"
          onClick={onClear}
          className="text-sm text-text-3 hover:text-text-2 underline underline-offset-2"
        >
          Clear
        </button>
      )}
      {trailing && <div className="ml-auto">{trailing}</div>}
    </div>
  );
}
