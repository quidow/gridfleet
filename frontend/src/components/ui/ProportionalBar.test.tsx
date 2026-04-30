import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import ProportionalBar from './ProportionalBar';

describe('ProportionalBar', () => {
  it('renders linked legend rows', () => {
    render(
      <MemoryRouter>
        <ProportionalBar
          segments={[
            {
              key: 'available',
              label: 'Available',
              count: 3,
              barClassName: 'bg-success-strong',
              to: '/devices?status=available',
            },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole('link', { name: /Available/i })).toHaveAttribute('href', '/devices?status=available');
    expect(screen.getByLabelText('Available: 3')).toBeInTheDocument();
  });

  it('renders plain text legend rows when no link is provided', () => {
    render(
      <ProportionalBar
        segments={[{ key: 'offline', label: 'Offline', count: 1, barClassName: 'bg-neutral-strong' }]}
      />,
    );

    expect(screen.getByText('Offline')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Offline/i })).not.toBeInTheDocument();
  });

  it('shows empty track when total is zero', () => {
    const { container } = render(
      <ProportionalBar
        segments={[{ key: 'offline', label: 'Offline', count: 0, barClassName: 'bg-neutral-strong' }]}
      />,
    );

    expect(screen.queryByLabelText('Offline: 0')).not.toBeInTheDocument();
    expect(container.querySelector('.bg-surface-2')).toBeInTheDocument();
  });

  it('omits the legend when showLegend is false', () => {
    render(
      <MemoryRouter>
        <ProportionalBar
          showLegend={false}
          segments={[
            { key: 'available', label: 'Available', count: 3, barClassName: 'bg-success-strong', to: '/devices' },
          ]}
        />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: /Available/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Available: 3')).toBeInTheDocument();
  });
});
