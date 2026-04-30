import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import Toggle from './Toggle';

describe('Toggle', () => {
  it('exposes role=switch and aria-checked', () => {
    render(<Toggle checked onChange={() => {}} aria-label="Auto" />);

    const toggle = screen.getByRole('switch', { name: 'Auto' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('emits next state on click', async () => {
    const onChange = vi.fn();

    render(<Toggle checked={false} onChange={onChange} aria-label="Auto" />);

    await userEvent.click(screen.getByRole('switch', { name: 'Auto' }));

    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('is keyboard accessible via Space', async () => {
    const onChange = vi.fn();

    render(<Toggle checked={false} onChange={onChange} aria-label="Auto" />);

    const toggle = screen.getByRole('switch', { name: 'Auto' });
    toggle.focus();
    await userEvent.keyboard(' ');

    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('respects disabled', () => {
    render(<Toggle checked={false} onChange={() => {}} aria-label="Auto" disabled />);

    expect(screen.getByRole('switch', { name: 'Auto' })).toBeDisabled();
  });
});
