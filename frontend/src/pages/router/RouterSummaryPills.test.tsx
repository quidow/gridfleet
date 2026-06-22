import { render, screen, within } from '@testing-library/react';
import { describe, it, expect } from 'vitest';

import { RouterSummaryPills } from './RouterSummaryPills';
import type { GridRouterCounts } from '../../types/gridRouter';

const counts: GridRouterCounts = {
  registered: 10,
  running: 7,
  available: 5,
  busy: 1,
  verifying: 2,
  offline: 1,
  maintenance: 1,
  eligible: 2,
  active_sessions: 4,
  queue_depth: 6,
};

const pillValue = (label: string) =>
  within(screen.getByText(label).parentElement as HTMLElement).getAllByText(/^\d+$/).at(-1)?.textContent;

describe('RouterSummaryPills', () => {
  it('collapses the operational states into routing buckets', () => {
    render(<RouterSummaryPills counts={counts} />);

    expect(pillValue('open')).toBe('2'); // eligible
    expect(pillValue('not ready')).toBe('3'); // available - eligible
    expect(pillValue('busy')).toBe('3'); // busy + verifying
    expect(pillValue('down')).toBe('2'); // offline + maintenance
    expect(pillValue('sessions')).toBe('4');
    expect(pillValue('queue')).toBe('6');
  });

  it('drops the redundant registered and running pills', () => {
    render(<RouterSummaryPills counts={counts} />);

    expect(screen.queryByText('registered')).not.toBeInTheDocument();
    expect(screen.queryByText('running')).not.toBeInTheDocument();
  });
});
