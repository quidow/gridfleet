import type { HTMLAttributes, ReactNode } from 'react';

type CardPadding = 'none' | 'sm' | 'md' | 'lg';

interface CardProps extends Omit<HTMLAttributes<HTMLElement>, 'children'> {
  padding?: CardPadding;
  as?: 'div' | 'section' | 'article';
  children: ReactNode;
}

const PADDING_CLASSES: Record<CardPadding, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-6',
};

export function Card({ padding = 'md', as: Tag = 'div', className = '', children, ...rest }: CardProps) {
  return (
    <Tag
      className={[
        'rounded-lg border border-border bg-surface-1 shadow-sm',
        PADDING_CLASSES[padding],
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {children}
    </Tag>
  );
}
