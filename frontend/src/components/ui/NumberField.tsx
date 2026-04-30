import { forwardRef, type InputHTMLAttributes, useEffect, useState } from 'react';
import TextField from './TextField';

interface NumberFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'type' | 'size'> {
  value: number | null;
  onChange: (value: number | null) => void;
  invalid?: boolean;
  size?: 'sm' | 'md';
  fullWidth?: boolean;
}

const NumberField = forwardRef<HTMLInputElement, NumberFieldProps>(function NumberField(
  { value, onChange, invalid, size, fullWidth, ...rest },
  ref,
) {
  const [draftValue, setDraftValue] = useState(() => (value ?? '').toString());

  useEffect(() => {
    setDraftValue((value ?? '').toString());
  }, [value]);

  return (
    <TextField
      ref={ref}
      type="number"
      inputMode="numeric"
      value={draftValue}
      onChange={(raw) => {
        setDraftValue(raw);

        if (raw === '') {
          onChange(null);
          return;
        }

        const parsed = Number(raw);
        onChange(Number.isFinite(parsed) ? parsed : null);
      }}
      invalid={invalid}
      size={size}
      fullWidth={fullWidth}
      {...rest}
    />
  );
});

export default NumberField;
