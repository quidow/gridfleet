import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AvailabilityCell } from './deviceColumns';
import type { DeviceRead } from '../../types';

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  return {
    operational_state: 'available',
    needs_attention: false,
    lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
    health_summary: {
      device: { status: 'ok', detail: null, checked_at: null },
      node: { status: 'ok', detail: 'running', checked_at: null },
      viability: { status: 'unknown', detail: 'not run', checked_at: null },
      overall: 'ok',
    },
    readiness_state: 'verified',
    missing_setup_fields: [],
    ...overrides,
  } as DeviceRead;
}

describe('AvailabilityCell', () => {
  it('renders availability label for available device', () => {
    render(<AvailabilityCell device={makeDevice()} />);
    expect(screen.getByText('Available')).toBeInTheDocument();
  });

  it('renders availability label for offline device', () => {
    render(<AvailabilityCell device={makeDevice({ operational_state: 'offline' })} />);
    expect(screen.getByText('Offline')).toBeInTheDocument();
  });
});
