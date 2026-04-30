import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import TextField from './TextField';

describe('TextField', () => {
  it('forwards value and change handler', async () => {
    const onChange = vi.fn();

    render(<TextField value="" onChange={onChange} aria-label="Name" />);

    await userEvent.type(screen.getByLabelText('Name'), 'a');

    expect(onChange).toHaveBeenCalledWith('a');
  });

  it('applies design-token focus ring class', () => {
    render(<TextField value="" onChange={() => {}} aria-label="Name" />);

    expect(screen.getByLabelText('Name').className).toMatch(/focus:ring-accent/);
  });

  it('respects disabled', () => {
    render(<TextField value="" onChange={() => {}} aria-label="Name" disabled />);

    expect(screen.getByLabelText('Name')).toBeDisabled();
  });

  it('passes through type=password', () => {
    render(<TextField value="" onChange={() => {}} aria-label="Pw" type="password" />);

    expect(screen.getByLabelText('Pw')).toHaveAttribute('type', 'password');
  });
});
