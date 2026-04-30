import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import ReservationPill from './ReservationPill';

describe('ReservationPill', () => {
  it('renders a link to the reserving run', () => {
    render(
      <MemoryRouter>
        <ReservationPill reservation={{ run_id: 'r1', run_name: 'nightly-regression', excluded: false }} />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', '/runs/r1');
    expect(link).toHaveTextContent('Reserved by nightly-regression');
  });

  it('appends "(excluded)" to the label when reservation is excluded', () => {
    render(
      <MemoryRouter>
        <ReservationPill reservation={{ run_id: 'r1', run_name: 'nightly-regression', excluded: true }} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link')).toHaveTextContent('Reserved by nightly-regression (excluded)');
  });

  it('includes a lock icon inside the link', () => {
    const { container } = render(
      <MemoryRouter>
        <ReservationPill reservation={{ run_id: 'r1', run_name: 'x', excluded: false }} />
      </MemoryRouter>,
    );
    expect(container.querySelector('svg.lucide-lock-keyhole')).not.toBeNull();
  });
});
