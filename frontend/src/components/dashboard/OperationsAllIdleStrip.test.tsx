import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OperationsAllIdleStrip from './OperationsAllIdleStrip';

describe('OperationsAllIdleStrip', () => {
  it('announces idle state with role=status', () => {
    render(<MemoryRouter><OperationsAllIdleStrip /></MemoryRouter>);
    const el = screen.getByRole('status');
    expect(el).toHaveTextContent(/no active runs/i);
    expect(el).toHaveTextContent(/no busy devices/i);
  });

  it('renders the two view-links when hrefs are provided', () => {
    render(
      <MemoryRouter>
        <OperationsAllIdleStrip runsHref="/runs" devicesHref="/devices?status=busy" />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: /view runs/i })).toHaveAttribute('href', '/runs');
    expect(screen.getByRole('link', { name: /view busy/i })).toHaveAttribute('href', '/devices?status=busy');
  });
});
