import type { InputHTMLAttributes } from 'react';
import { isoToDateOnly } from '../../utils/dateFormatting';

type DateInputSize = 'sm' | 'md';

interface DateInputOwnProps {
  value: string;
  onChange: (value: string) => void;
  ariaLabel?: string;
  size?: DateInputSize;
  fullWidth?: boolean;
}

type DateInputProps = DateInputOwnProps &
  Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'size' | 'type'>;

const SIZE_CLASSES: Record<DateInputSize, string> = {
  md: 'px-3 py-2 text-sm',
  sm: 'px-2 py-1.5 text-xs',
};

export default function DateInput({
  value,
  onChange,
  ariaLabel,
  size = 'md',
  fullWidth = false,
  className = '',
  min,
  max,
  ...rest
}: DateInputProps) {
  return (
    <input
      type="date"
      value={isoToDateOnly(value)}
      onChange={(event) => onChange(event.target.value)}
      aria-label={ariaLabel}
      min={min ? isoToDateOnly(String(min)) || String(min) : undefined}
      max={max ? isoToDateOnly(String(max)) || String(max) : undefined}
      className={[
        'border border-border-strong rounded-md bg-surface-1 text-text-1 focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-60 disabled:cursor-not-allowed',
        SIZE_CLASSES[size],
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    />
  );
}
