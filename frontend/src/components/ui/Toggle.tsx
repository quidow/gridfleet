import type { ButtonHTMLAttributes } from 'react';

interface ToggleProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'type' | 'aria-checked'> {
  checked: boolean;
  onChange: (checked: boolean) => void;
}

export default function Toggle({ checked, onChange, className = '', disabled, ...rest }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-accent disabled:cursor-not-allowed disabled:opacity-60',
        checked ? 'bg-accent' : 'bg-border-strong',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      <span
        className={[
          'inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0.5',
        ].join(' ')}
      />
    </button>
  );
}
