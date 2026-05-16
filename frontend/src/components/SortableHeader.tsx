import { ArrowUpDown, ChevronDown, ChevronUp } from 'lucide-react';

type SortDirection = 'asc' | 'desc';

interface SortableHeaderProps {
  label: string;
  active: boolean;
  direction: SortDirection;
  onToggle: () => void;
  align?: 'left' | 'center' | 'right';
}

export function SortableHeader({
  label,
  active,
  direction,
  onToggle,
  align = 'left',
}: SortableHeaderProps) {
  const justifyClass =
    align === 'right' ? 'justify-end' : align === 'center' ? 'justify-center' : 'justify-start';

  return (
    <button
      type="button"
      onClick={onToggle}
      className={`inline-flex w-full items-center gap-1 ${justifyClass} text-left text-xs font-medium uppercase text-text-2 hover:text-text-2`}
    >
      <span>{label}</span>
      {active ? (
        direction === 'asc' ? <ChevronUp size={14} /> : <ChevronDown size={14} />
      ) : (
        <ArrowUpDown size={14} />
      )}
    </button>
  );
}
