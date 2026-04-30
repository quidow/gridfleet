import type { ReactNode } from 'react';

export interface DefinitionListItem {
  term: ReactNode;
  definition: ReactNode;
  dense?: boolean;
}

type DefinitionListLayout = 'justified' | 'stacked';

interface DefinitionListProps {
  items: DefinitionListItem[];
  layout?: DefinitionListLayout;
  className?: string;
}

export default function DefinitionList({
  items,
  layout = 'justified',
  className = '',
}: DefinitionListProps) {
  const spacing = layout === 'justified' ? 'space-y-2' : 'space-y-3';
  return (
    <dl className={[`text-sm ${spacing}`, className].filter(Boolean).join(' ')}>
      {items.map((item, idx) => {
        const rowClass =
          layout === 'justified'
            ? item.dense
              ? 'flex justify-between gap-4 py-0.5'
              : 'flex justify-between gap-4'
            : item.dense
              ? 'flex flex-col gap-0.5'
              : 'flex flex-col gap-1';
        return (
          <div key={idx} className={rowClass}>
            <dt className="text-text-3">{item.term}</dt>
            <dd
              className={
                layout === 'justified' ? 'text-text-1 text-right' : 'text-text-1'
              }
            >
              {item.definition}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
