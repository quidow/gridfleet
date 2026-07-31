import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { StateHistoryPanel } from './StateHistoryPanel';
import type { LifecycleIncidentRead } from '../../types';

let items: LifecycleIncidentRead[] = [];

vi.mock('../../hooks/useCursorQueryState', () => ({
  useCursorQueryState: () => ({
    pageSize: 25,
    cursor: '',
    direction: 'older',
    setPageSize: vi.fn(),
    goOlder: vi.fn(),
    goNewer: vi.fn(),
    resetToNewest: vi.fn(),
  }),
}));

vi.mock('../../hooks/useLifecycle', () => ({
  useLifecycleIncidents: () => ({
    data: { items, limit: 25, next_cursor: null, prev_cursor: null },
    isLoading: false,
  }),
}));

function incident(overrides: Partial<LifecycleIncidentRead>): LifecycleIncidentRead {
  return {
    id: 'event-1',
    device_id: 'device-1',
    device_name: 'Device One',
    device_identity_value: 'device-serial-1',
    platform_id: 'android_mobile',
    event_type: 'health_check_fail',
    label: 'Health Fail',
    summary_state: 'idle',
    reason: null,
    detail: null,
    source: null,
    run_id: null,
    run_name: null,
    backoff_until: null,
    created_at: '2026-07-31T10:00:00Z',
    ...overrides,
  };
}

describe('StateHistoryPanel', () => {
  it('renders badges for failure and maintenance event types', () => {
    items = [
      incident({ id: 'event-1', event_type: 'maintenance_exited', created_at: '2026-07-31T10:06:00Z' }),
      incident({
        id: 'event-2',
        event_type: 'maintenance_entered',
        reason: 'run escalation',
        created_at: '2026-07-31T10:05:00Z',
      }),
      incident({ id: 'event-3', event_type: 'node_restart', created_at: '2026-07-31T10:04:00Z' }),
      incident({ id: 'event-4', event_type: 'connectivity_lost', created_at: '2026-07-31T10:03:00Z' }),
      incident({ id: 'event-5', event_type: 'health_check_fail', created_at: '2026-07-31T10:02:00Z' }),
    ];

    render(<StateHistoryPanel deviceId="device-1" />);

    expect(screen.getByText('Maintenance Exited')).toBeInTheDocument();
    expect(screen.getByText('Maintenance Entered')).toBeInTheDocument();
    expect(screen.getByText('Node Restart')).toBeInTheDocument();
    expect(screen.getByText('Disconnected')).toBeInTheDocument();
    expect(screen.getByText('Health Fail')).toBeInTheDocument();
    expect(screen.getByText('run escalation')).toBeInTheDocument();
  });

  it('falls back to the raw event type for unmapped types', () => {
    items = [incident({ event_type: 'hardware_health_changed' })];

    render(<StateHistoryPanel deviceId="device-1" />);

    expect(screen.getByText('hardware_health_changed')).toBeInTheDocument();
  });
});
