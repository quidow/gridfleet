import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DeviceStatusCard from './DeviceStatusCard';
import type { DeviceRead } from '../../types';

function makeDevice(overrides: Partial<DeviceRead> = {}): DeviceRead {
  return {
    availability_status: 'offline',
    lifecycle_policy_summary: {
      state: 'suppressed',
      label: 'Suppressed',
      detail: 'Node restart failed',
      backoff_until: null,
    },
    readiness_state: 'verified',
    missing_setup_fields: [],
    health_summary: { healthy: false, summary: 'Disconnected', last_checked_at: null },
    needs_attention: true,
    ...overrides,
  } as unknown as DeviceRead;
}

describe('DeviceStatusCard', () => {
  it('renders narrative + action buttons for suppressed offline device', () => {
    const onRetry = vi.fn();
    const onMaintenance = vi.fn();
    render(
      <DeviceStatusCard
        device={makeDevice()}
        onRetry={onRetry}
        onMaintenance={onMaintenance}
        onSetup={() => {}}
        onVerify={() => {}}
        onExitMaintenance={() => {}}
      />,
    );
    expect(screen.getByText(/admin/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /retry now/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /maintenance/i }));
    expect(onMaintenance).toHaveBeenCalledTimes(1);
  });

  it('renders narrative without buttons for healthy available device', () => {
    render(
      <DeviceStatusCard
        device={makeDevice({
          availability_status: 'available',
          lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
          needs_attention: false,
          health_summary: { healthy: true, summary: 'Healthy', last_checked_at: null },
        })}
        onRetry={() => {}}
        onMaintenance={() => {}}
        onSetup={() => {}}
        onVerify={() => {}}
        onExitMaintenance={() => {}}
      />,
    );
    expect(screen.getByText(/available/i)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('clicking the maintenance action calls onExitMaintenance, not onRetry', () => {
    const onRetry = vi.fn();
    const onExitMaintenance = vi.fn();
    render(
      <DeviceStatusCard
        device={makeDevice({
          availability_status: 'maintenance',
          lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
          needs_attention: false,
          health_summary: { healthy: true, summary: 'Healthy', last_checked_at: null },
        })}
        onRetry={onRetry}
        onMaintenance={() => {}}
        onSetup={() => {}}
        onVerify={() => {}}
        onExitMaintenance={onExitMaintenance}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /take out of maintenance/i }));
    expect(onExitMaintenance).toHaveBeenCalledTimes(1);
    expect(onRetry).not.toHaveBeenCalled();
  });
});
