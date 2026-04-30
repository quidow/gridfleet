import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import Select from './Select';

describe('Select', () => {
  it('renders options from props', () => {
    render(
      <Select
        value="a"
        onChange={() => {}}
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta' },
        ]}
      />,
    );
    expect(screen.getByRole('option', { name: 'Alpha' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Beta' })).toBeInTheDocument();
  });

  it('renders placeholder option when provided', () => {
    render(
      <Select
        value=""
        onChange={() => {}}
        placeholder="All"
        options={[{ value: 'a', label: 'Alpha' }]}
      />,
    );
    const placeholder = screen.getByRole('option', { name: 'All' }) as HTMLOptionElement;
    expect(placeholder.value).toBe('');
  });

  it('calls onChange with the new value', () => {
    const onChange = vi.fn();
    render(
      <Select
        value="a"
        onChange={onChange}
        ariaLabel="choice"
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta' },
        ]}
      />,
    );
    fireEvent.change(screen.getByLabelText('choice'), { target: { value: 'b' } });
    expect(onChange).toHaveBeenCalledWith('b');
  });

  it('supports inline children when options not supplied', () => {
    render(
      <Select value="x" onChange={() => {}} ariaLabel="inline">
        <option value="x">X</option>
        <option value="y">Y</option>
      </Select>,
    );
    expect(screen.getByRole('option', { name: 'X' })).toBeInTheDocument();
  });

  it('marks disabled options', () => {
    render(
      <Select
        value="a"
        onChange={() => {}}
        options={[
          { value: 'a', label: 'Alpha' },
          { value: 'b', label: 'Beta', disabled: true },
        ]}
      />,
    );
    expect((screen.getByRole('option', { name: 'Beta' }) as HTMLOptionElement).disabled).toBe(true);
  });

  it('applies shared border + focus-ring classes', () => {
    render(<Select value="" onChange={() => {}} ariaLabel="styled" options={[]} />);
    const select = screen.getByLabelText('styled');
    expect(select.className).toMatch(/border-border-strong/);
    expect(select.className).toMatch(/focus:ring-accent/);
  });
});
