import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import DashboardCard from './DashboardCard';

describe('DashboardCard', () => {
  it('renders header, slots, body, and footer', () => {
    render(
      <DashboardCard
        title="Device recovery"
        titleSlot={<span>2 affected</span>}
        description="Lifecycle actions and recovery state."
        rightSlot={<button type="button">Investigate</button>}
        footer={<div>Footer metrics</div>}
      >
        <div>Card body</div>
      </DashboardCard>,
    );

    expect(screen.getByRole('heading', { name: 'Device recovery' })).toBeInTheDocument();
    expect(screen.getByText('2 affected')).toBeInTheDocument();
    expect(screen.getByText('Lifecycle actions and recovery state.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Investigate' })).toBeInTheDocument();
    expect(screen.getByText('Card body')).toBeInTheDocument();
    expect(screen.getByText('Footer metrics')).toBeInTheDocument();
  });

  it('marks primary cards with heavier chrome', () => {
    const { container } = render(
      <DashboardCard title="Fleet command center" variant="primary">
        <div>Primary body</div>
      </DashboardCard>,
    );

    const card = container.querySelector('[data-dashboard-card-variant="primary"]');
    expect(card).toBeInTheDocument();
    expect(card).toHaveClass('shadow-sm');
  });

  it('marks secondary cards without primary shadow', () => {
    const { container } = render(
      <DashboardCard title="Operations" variant="secondary">
        <div>Secondary body</div>
      </DashboardCard>,
    );

    const card = container.querySelector('[data-dashboard-card-variant="secondary"]');
    expect(card).toBeInTheDocument();
    expect(card).not.toHaveClass('shadow-sm');
  });
});
