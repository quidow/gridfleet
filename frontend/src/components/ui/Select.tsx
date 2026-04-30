import type { ReactNode, SelectHTMLAttributes } from 'react';

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

type SelectSize = 'sm' | 'md';

interface SelectOwnProps {
  value: string;
  onChange: (value: string) => void;
  options?: SelectOption[];
  placeholder?: string;
  size?: SelectSize;
  ariaLabel?: string;
  fullWidth?: boolean;
  children?: ReactNode;
}

type SelectProps = SelectOwnProps &
  Omit<SelectHTMLAttributes<HTMLSelectElement>, 'value' | 'onChange' | 'size' | 'children'>;

const SIZE_CLASSES: Record<SelectSize, string> = {
  md: 'px-3 py-2 text-sm',
  sm: 'px-2 py-1.5 text-xs',
};

export default function Select({
  value,
  onChange,
  options,
  placeholder,
  size = 'md',
  ariaLabel,
  fullWidth = false,
  className = '',
  children,
  ...rest
}: SelectProps) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label={ariaLabel}
      className={[
        'border border-border-strong rounded-md bg-surface-1 text-text-1 focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-60 disabled:cursor-not-allowed',
        SIZE_CLASSES[size],
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options
        ? options.map((opt) => (
            <option key={opt.value} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))
        : children}
    </select>
  );
}
