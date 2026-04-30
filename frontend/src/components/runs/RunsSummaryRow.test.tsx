import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import RunsSummaryRow from './RunsSummaryRow';

const baseRun = (overrides: Record<string, unknown>) => ({
  id: 'r', name: 'r', state: 'pending', requirements: [],
  ttl_minutes: 60, heartbeat_timeout_sec: 120,
  reserved_devices: null, error: null,
  created_at: '2026-04-19T10:00:00Z', started_at: null,
  completed_at: null, created_by: null, last_heartbeat: null,
  session_counts: { passed: 0, failed: 0, error: 0, running: 0, total: 0 },
  ...overrides,
});

describe('RunsSummaryRow', () => {
  it('renders summary pills for running, queued, passed, and failed', () => {
    render(<RunsSummaryRow currentPageRuns={[]} last24hRuns={[]} now={new Date('2026-04-19T12:00:00Z')} />);
    expect(screen.getByText('Running').parentElement?.tagName).toBe('SPAN');
    expect(screen.getByText('Queued').parentElement?.tagName).toBe('SPAN');
    expect(screen.getByText('Passed 24H').parentElement?.tagName).toBe('SPAN');
    expect(screen.getByText('Failed 24H').parentElement?.tagName).toBe('SPAN');
  });

  it('uses current-page runs for Running and Queued; 24h list for pass/fail sums', () => {
    const currentPageRuns = [
      baseRun({ id: 'a', state: 'active' }),
      baseRun({ id: 'b', state: 'pending' }),
    ];
    const last24hRuns = [
      baseRun({
        id: 'c', state: 'completed', completed_at: '2026-04-19T11:30:00Z',
        session_counts: { passed: 7, failed: 1, error: 0, running: 0, total: 8 },
      }),
    ];
    render(
      <RunsSummaryRow
        currentPageRuns={currentPageRuns as never}
        last24hRuns={last24hRuns as never}
        now={new Date('2026-04-19T12:00:00Z')}
      />,
    );
    expect(screen.getByText('Running').parentElement).toHaveTextContent('1');
    expect(screen.getByText('Queued').parentElement).toHaveTextContent('1');
    expect(screen.getByText('Passed 24H').parentElement).toHaveTextContent('7');
    expect(screen.getByText('Failed 24H').parentElement).toHaveTextContent('1');
  });

  it('renders dashes when last24hRuns is undefined (loading)', () => {
    render(<RunsSummaryRow currentPageRuns={[]} last24hRuns={undefined} now={new Date('2026-04-19T12:00:00Z')} />);
    expect(screen.getByText('Passed 24H').parentElement).toHaveTextContent('—');
    expect(screen.getByText('Failed 24H').parentElement).toHaveTextContent('—');
  });
});
