import { forwardRef, type TextareaHTMLAttributes } from 'react';

interface TextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'onChange'> {
  value: string;
  onChange: (value: string) => void;
  monospace?: boolean;
  invalid?: boolean;
  fullWidth?: boolean;
}

const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { value, onChange, monospace = false, invalid = false, fullWidth = true, className = '', rows = 4, ...rest },
  ref,
) {
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-invalid={invalid || undefined}
      rows={rows}
      className={[
        'rounded-md border bg-surface-1 px-3 py-2 text-sm text-text-1 shadow-sm outline-none transition focus:ring-2 disabled:cursor-not-allowed disabled:opacity-60',
        invalid
          ? 'border-danger-strong focus:border-danger-strong focus:ring-danger-strong'
          : 'border-border-strong focus:border-accent focus:ring-accent',
        monospace ? 'font-mono' : '',
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    />
  );
});

export default Textarea;
