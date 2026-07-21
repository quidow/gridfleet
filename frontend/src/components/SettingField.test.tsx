import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingField } from './SettingField';
import type { SettingRead } from '../types';

function makeSetting(overrides: Partial<SettingRead> = {}): SettingRead {
  return {
    key: 'device_checks.ip_ping.timeout_sec',
    type: 'float',
    value: 2.0,
    default_value: 2.0,
    description: 'Per-attempt ICMP-ping timeout used by the adapter.',
    category: 'device_checks',
    is_overridden: false,
    validation: { min: 0.5, max: 30.0 },
    ...overrides,
  } as SettingRead;
}

describe('SettingField', () => {
  it('renders an input for float settings', () => {
    render(
      <SettingField setting={makeSetting()} value={2.0} onChange={vi.fn()} onReset={vi.fn()} />,
    );
    const input = screen.getByLabelText('Timeout Sec');
    expect(input).toHaveValue(2);
    expect(input).toHaveAttribute('step', 'any');
  });

  it('surfaces a setting type it cannot render instead of showing nothing', () => {
    const unknownType = makeSetting({ type: 'duration' as SettingRead['type'], value: '5m' });
    render(
      <SettingField setting={unknownType} value="5m" onChange={vi.fn()} onReset={vi.fn()} />,
    );
    expect(screen.getByText(/unsupported setting type: duration/i)).toBeInTheDocument();
  });

  it('reports out-of-range float values', () => {
    render(
      <SettingField setting={makeSetting()} value={99.5} onChange={vi.fn()} onReset={vi.fn()} />,
    );
    expect(screen.getByText('Maximum value is 30')).toBeInTheDocument();
  });
});
