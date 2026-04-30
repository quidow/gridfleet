import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import Field from './Field';

describe('Field', () => {
  it('renders label associated with child control via htmlFor/id', () => {
    render(
      <Field label="Name" htmlFor="name">
        <input id="name" />
      </Field>,
    );

    const label = screen.getByText('Name');
    expect(label).toHaveAttribute('for', 'name');
  });

  it('renders hint text when provided', () => {
    render(
      <Field label="Name" hint="Max 50 characters">
        <input />
      </Field>,
    );

    expect(screen.getByText('Max 50 characters')).toBeInTheDocument();
  });

  it('renders error in danger tone and hides hint', () => {
    render(
      <Field label="Name" hint="Hint" error="Required">
        <input />
      </Field>,
    );

    expect(screen.getByText('Required')).toHaveClass('text-danger-foreground');
    expect(screen.queryByText('Hint')).toBeNull();
  });

  it('applies required asterisk when required', () => {
    render(
      <Field label="Name" required>
        <input />
      </Field>,
    );

    expect(screen.getByText('Name').textContent).toContain('*');
  });
});
