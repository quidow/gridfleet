import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FilterBar from './FilterBar';

describe('FilterBar', () => {
  it('renders children', () => {
    render(
      <FilterBar>
        <span>filter-child</span>
      </FilterBar>,
    );
    expect(screen.getByText('filter-child')).toBeDefined();
  });

  it('shows the Clear button when onClear is provided', () => {
    render(
      <FilterBar onClear={vi.fn()}>
        <span>child</span>
      </FilterBar>,
    );
    expect(screen.getByRole('button', { name: /clear/i })).toBeDefined();
  });

  it('does not show the Clear button when onClear is not provided', () => {
    render(
      <FilterBar>
        <span>child</span>
      </FilterBar>,
    );
    expect(screen.queryByRole('button', { name: /clear/i })).toBeNull();
  });

  it('calls onClear when the Clear button is clicked', async () => {
    const onClear = vi.fn();
    render(
      <FilterBar onClear={onClear}>
        <span>child</span>
      </FilterBar>,
    );
    await userEvent.click(screen.getByRole('button', { name: /clear/i }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it('renders a trailing slot', () => {
    render(
      <FilterBar trailing={<span>trailing-content</span>}>
        <span>child</span>
      </FilterBar>,
    );
    expect(screen.getByText('trailing-content')).toBeDefined();
  });
});
