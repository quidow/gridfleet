import { forwardRef, type InputHTMLAttributes } from 'react';

type TextFieldSize = 'sm' | 'md';

interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'size'> {
  value: string | number;
  onChange: (value: string) => void;
  size?: TextFieldSize;
  invalid?: boolean;
  fullWidth?: boolean;
}

const SIZE_CLASSES: Record<TextFieldSize, string> = {
  md: 'px-3 py-2 text-sm',
  sm: 'px-2.5 py-1.5 text-xs',
};

const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  { value, onChange, size = 'md', invalid = false, fullWidth = true, className = '', type = 'text', ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      type={type}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-invalid={invalid || undefined}
      className={[
        'rounded-md border bg-surface-1 text-text-1 shadow-sm outline-none transition focus:ring-2 disabled:cursor-not-allowed disabled:opacity-60',
        invalid
          ? 'border-danger-strong focus:border-danger-strong focus:ring-danger-strong'
          : 'border-border-strong focus:border-accent focus:ring-accent',
        SIZE_CLASSES[size],
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    />
  );
});

export default TextField;
