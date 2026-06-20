import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { QueueTile } from './QueueTile';
import type { GridRouterRead } from '../../types/gridRouter';

function renderTile(queue: GridRouterRead['queue']) {
  render(
    <MemoryRouter>
      <QueueTile queue={queue} />
    </MemoryRouter>,
  );
}

describe('QueueTile', () => {
  it('shows an empty message when there are no requests', () => {
    renderTile([]);
    expect(screen.getByText('No queued requests.')).toBeInTheDocument();
    expect(screen.getByText('Queue (0)')).toBeInTheDocument();
  });

  it('lists waiting requests with their capabilities', () => {
    renderTile([
      {
        requestId: 'q1',
        capabilities: { platformName: 'Android' },
        requestTimestamp: new Date().toISOString(),
        runId: null,
      },
    ]);
    expect(screen.getByText('Queue (1)')).toBeInTheDocument();
    expect(screen.getByText(/platformName/)).toBeInTheDocument();
    expect(screen.getByText('free')).toBeInTheDocument();
  });

  it('links to the run when a request is bound to one', () => {
    renderTile([
      {
        requestId: 'q2',
        capabilities: { platformName: 'iOS' },
        requestTimestamp: new Date().toISOString(),
        runId: 'r1',
      },
    ]);
    const link = screen.getByRole('link', { name: 'run' });
    expect(link).toHaveAttribute('href', '/runs/r1');
  });
});
