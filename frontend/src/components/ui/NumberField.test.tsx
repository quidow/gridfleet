import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import NumberField from './NumberField';

describe('NumberField', () => {
  it('emits number when typing digits', async () => {
    const onChange = vi.fn();

    render(<NumberField value={0} onChange={onChange} aria-label="Timeout" />);

    await userEvent.clear(screen.getByLabelText('Timeout'));
    await userEvent.type(screen.getByLabelText('Timeout'), '12');

    expect(onChange).toHaveBeenLastCalledWith(12);
  });

  it('emits null when cleared', async () => {
    const onChange = vi.fn();

    render(<NumberField value={5} onChange={onChange} aria-label="Timeout" />);

    await userEvent.clear(screen.getByLabelText('Timeout'));

    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('respects min and max attributes', () => {
    render(<NumberField value={1} onChange={() => {}} min={1} max={10} aria-label="Timeout" />);

    const input = screen.getByLabelText('Timeout');
    expect(input).toHaveAttribute('min', '1');
    expect(input).toHaveAttribute('max', '10');
  });
});
