import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import Textarea from './Textarea';

describe('Textarea', () => {
  it('forwards value changes', async () => {
    const onChange = vi.fn();

    render(<Textarea value="" onChange={onChange} aria-label="Tags" />);

    await userEvent.type(screen.getByLabelText('Tags'), 'x');

    expect(onChange).toHaveBeenCalledWith('x');
  });

  it('applies mono font when monospace prop is set', () => {
    render(<Textarea value="{}" onChange={() => {}} monospace aria-label="Tags" />);

    expect(screen.getByLabelText('Tags').className).toMatch(/font-mono/);
  });

  it('supports invalid state', () => {
    render(<Textarea value="" onChange={() => {}} invalid aria-label="Tags" />);

    expect(screen.getByLabelText('Tags')).toHaveAttribute('aria-invalid', 'true');
  });
});
