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
  it('renders the API-supplied label for every event type', () => {
    items = [
      incident({ id: 'event-1', event_type: 'maintenance_exited', label: 'Maintenance Exited', created_at: '2026-07-31T10:06:00Z' }),
      incident({
        id: 'event-2',
        event_type: 'maintenance_entered',
        label: 'Maintenance Entered',
        reason: 'run escalation',
        created_at: '2026-07-31T10:05:00Z',
      }),
      incident({ id: 'event-3', event_type: 'node_restart', label: 'Node Restart', created_at: '2026-07-31T10:04:00Z' }),
      incident({ id: 'event-4', event_type: 'connectivity_lost', label: 'Disconnected', created_at: '2026-07-31T10:03:00Z' }),
      incident({ id: 'event-5', event_type: 'health_check_fail', label: 'Health Fail', created_at: '2026-07-31T10:02:00Z' }),
    ];

    render(<StateHistoryPanel deviceId="device-1" />);

    expect(screen.getByText('Maintenance Exited')).toBeInTheDocument();
    expect(screen.getByText('Maintenance Entered')).toBeInTheDocument();
    expect(screen.getByText('Node Restart')).toBeInTheDocument();
    expect(screen.getByText('Disconnected')).toBeInTheDocument();
    expect(screen.getByText('Health Fail')).toBeInTheDocument();
    expect(screen.getByText('run escalation')).toBeInTheDocument();
  });

  it('renders cooldown labels that the old local badge map was missing', () => {
    items = [
      incident({ id: 'event-1', event_type: 'lifecycle_run_cooldown_set', label: 'Run Cooldown', created_at: '2026-07-31T10:01:00Z' }),
      incident({
        id: 'event-2',
        event_type: 'lifecycle_run_cooldown_escalated',
        label: 'Cooldown Extended',
        created_at: '2026-07-31T10:00:00Z',
      }),
    ];

    render(<StateHistoryPanel deviceId="device-1" />);

    expect(screen.getByText('Run Cooldown')).toBeInTheDocument();
    expect(screen.getByText('Cooldown Extended')).toBeInTheDocument();
    expect(screen.queryByText('lifecycle_run_cooldown_set')).not.toBeInTheDocument();
  });
});
