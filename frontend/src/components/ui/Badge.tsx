import type { HTMLAttributes, ReactNode } from 'react';

export type BadgeTone = 'neutral' | 'info' | 'success' | 'warning' | 'critical';
type BadgeSize = 'sm' | 'md';

interface BadgeProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'children'> {
  tone?: BadgeTone;
  size?: BadgeSize;
  icon?: ReactNode;
  dot?: boolean;
  children: ReactNode;
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: 'bg-neutral-soft text-neutral-foreground',
  info: 'bg-info-soft text-info-foreground',
  success: 'bg-success-soft text-success-foreground',
  warning: 'bg-warning-soft text-warning-foreground',
  critical: 'bg-danger-soft text-danger-foreground',
};

const DOT_CLASSES: Record<BadgeTone, string> = {
  neutral: 'bg-neutral-strong',
  info: 'bg-info-strong',
  success: 'bg-success-strong',
  warning: 'bg-warning-strong',
  critical: 'bg-danger-strong',
};

const SIZE_CLASSES: Record<BadgeSize, string> = {
  md: 'px-2.5 py-0.5 text-xs',
  sm: 'px-1.5 py-0.5 text-xs',
};

// Compat alias: 'danger' was renamed to 'critical'. Remove once eventRegistry.ts is updated (Task 13).
const TONE_COMPAT: Partial<Record<string, BadgeTone>> = { danger: 'critical' };

export default function Badge({
  tone = 'neutral',
  size = 'md',
  icon,
  dot = false,
  className = '',
  children,
  ...rest
}: BadgeProps) {
  const resolvedTone: BadgeTone = TONE_COMPAT[tone as string] ?? tone;
  return (
    <span
      {...rest}
      className={[
        'inline-flex items-center gap-1 rounded-full font-medium',
        TONE_CLASSES[resolvedTone],
        SIZE_CLASSES[size],
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {dot && (
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${DOT_CLASSES[resolvedTone]}`} aria-hidden="true" />
      )}
      {icon}
      {children}
    </span>
  );
}
