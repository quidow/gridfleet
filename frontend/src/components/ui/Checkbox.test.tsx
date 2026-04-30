import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import Checkbox from './Checkbox';

describe('Checkbox', () => {
  it('renders label and toggles checked state', async () => {
    const onChange = vi.fn();

    render(<Checkbox checked={false} onChange={onChange} label="Auto-manage" />);

    await userEvent.click(screen.getByLabelText('Auto-manage'));

    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('applies accent color class', () => {
    render(<Checkbox checked onChange={() => {}} label="A" />);

    expect(screen.getByLabelText('A').className).toMatch(/accent-accent|text-accent/);
  });

  it('respects disabled', () => {
    render(<Checkbox checked={false} onChange={() => {}} label="A" disabled />);

    expect(screen.getByLabelText('A')).toBeDisabled();
  });
});
