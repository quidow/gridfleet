import type { ReactNode } from 'react';

type CardPadding = 'none' | 'sm' | 'md' | 'lg';

interface CardProps {
  padding?: CardPadding;
  as?: 'div' | 'section' | 'article';
  className?: string;
  children: ReactNode;
}

const PADDING_CLASSES: Record<CardPadding, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-6',
};

export default function Card({ padding = 'md', as: Tag = 'div', className = '', children }: CardProps) {
  return (
    <Tag
      className={[
        'rounded-lg border border-border bg-surface-1 shadow-sm',
        PADDING_CLASSES[padding],
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
    </Tag>
  );
}
