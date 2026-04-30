import type { ReactNode } from 'react';

interface FieldProps {
  label: string;
  htmlFor?: string;
  hint?: string;
  error?: string | null;
  required?: boolean;
  children: ReactNode;
  className?: string;
}

export default function Field({
  label,
  htmlFor,
  hint,
  error,
  required = false,
  children,
  className = '',
}: FieldProps) {
  return (
    <div className={['flex flex-col gap-1.5', className].filter(Boolean).join(' ')}>
      <label htmlFor={htmlFor} className="text-sm font-medium text-text-2">
        {label}
        {required ? <span aria-hidden="true"> *</span> : null}
      </label>
      {children}
      {error ? (
        <p className="text-xs text-danger-foreground">{error}</p>
      ) : hint ? (
        <p className="text-xs text-text-3">{hint}</p>
      ) : null}
    </div>
  );
}
