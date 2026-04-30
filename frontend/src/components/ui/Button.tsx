import type { ButtonHTMLAttributes, ReactNode } from 'react';

type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';
type ButtonSize = 'sm' | 'md';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
  fullWidth?: boolean;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-accent-on hover:bg-accent-hover focus:ring-accent disabled:opacity-60',
  secondary:
    'bg-surface-1 border border-border-strong text-text-2 hover:bg-surface-2 focus:ring-accent disabled:opacity-50',
  danger:
    'bg-danger-strong text-danger-on hover:bg-danger-foreground focus:ring-danger-strong disabled:opacity-60',
  ghost:
    'text-text-2 hover:bg-surface-2 focus:ring-accent disabled:opacity-50',
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  md: 'px-4 py-2 text-sm',
  sm: 'px-3 py-1.5 text-sm',
};

export default function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  leadingIcon,
  trailingIcon,
  fullWidth = false,
  children,
  disabled,
  className = '',
  type = 'button',
  ...rest
}: ButtonProps) {
  const isDisabled = disabled ?? loading;

  return (
    <button
      type={type}
      disabled={isDisabled}
      className={[
        'inline-flex items-center justify-center gap-2 rounded-md font-medium focus:outline-none focus:ring-2 focus:ring-offset-1 transition-colors cursor-pointer disabled:cursor-not-allowed',
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
        fullWidth ? 'w-full' : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {loading ? (
        <span className="flex items-center gap-2">
          <LoadingSpinnerInline />
          {children}
        </span>
      ) : (
        <>
          {leadingIcon}
          {children}
          {trailingIcon}
        </>
      )}
    </button>
  );
}

function LoadingSpinnerInline() {
  return (
    <svg
      className="h-4 w-4 animate-spin text-current opacity-75"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}
