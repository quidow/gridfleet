import { useId, type InputHTMLAttributes, type ReactNode } from 'react';

interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange' | 'checked'> {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: ReactNode;
  description?: ReactNode;
}

export default function Checkbox({
  checked,
  onChange,
  label,
  description,
  className = '',
  id,
  ...rest
}: CheckboxProps) {
  const autoId = useId();
  const checkboxId = id ?? autoId;

  return (
    <label className={['flex items-start gap-2 text-sm text-text-2', className].filter(Boolean).join(' ')}>
      <input
        id={checkboxId}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 h-4 w-4 rounded border-border-strong accent-accent text-accent focus:outline-none focus:ring-2 focus:ring-accent disabled:opacity-60"
        aria-label={typeof label === 'string' ? label : undefined}
        {...rest}
      />
      <span className="flex flex-col">
        <span className="text-text-1">{label}</span>
        {description ? <span className="text-xs text-text-3">{description}</span> : null}
      </span>
    </label>
  );
}
