import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DateInput from './DateInput';

describe('DateInput', () => {
  it('renders with type=date', () => {
    render(<DateInput value="2026-04-20" onChange={() => {}} ariaLabel="start" />);
    const input = screen.getByLabelText('start') as HTMLInputElement;
    expect(input.type).toBe('date');
    expect(input.value).toBe('2026-04-20');
  });

  it('normalises non-ISO-date-only strings to YYYY-MM-DD', () => {
    render(
      <DateInput value="2026-04-20T10:00:00Z" onChange={() => {}} ariaLabel="norm" />,
    );
    expect((screen.getByLabelText('norm') as HTMLInputElement).value).toBe('2026-04-20');
  });

  it('renders empty string when value is blank', () => {
    render(<DateInput value="" onChange={() => {}} ariaLabel="empty" />);
    expect((screen.getByLabelText('empty') as HTMLInputElement).value).toBe('');
  });

  it('calls onChange with raw YYYY-MM-DD', () => {
    const onChange = vi.fn();
    render(<DateInput value="2026-04-20" onChange={onChange} ariaLabel="change" />);
    fireEvent.change(screen.getByLabelText('change'), {
      target: { value: '2026-05-01' },
    });
    expect(onChange).toHaveBeenCalledWith('2026-05-01');
  });

  it('applies shared border + focus-ring classes', () => {
    render(<DateInput value="" onChange={() => {}} ariaLabel="styled" />);
    const input = screen.getByLabelText('styled');
    expect(input.className).toMatch(/border-border-strong/);
    expect(input.className).toMatch(/focus:ring-accent/);
  });
});
