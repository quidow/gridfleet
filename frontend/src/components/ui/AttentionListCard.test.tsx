import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import AttentionListCard from './AttentionListCard';

describe('AttentionListCard', () => {
  it('returns null when there are no rows', () => {
    const { container } = render(
      <AttentionListCard title="Attention" total={0} tone="neutral" rows={[]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders linked rows and description', () => {
    render(
      <MemoryRouter>
        <AttentionListCard
          title="Attention"
          description="devices needing review"
          total={2}
          tone="warn"
          rows={[{ label: 'Hardware health', values: '2 warn', to: '/devices?hardware_health_status=critical' }]}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText('devices needing review')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Hardware health/i })).toHaveAttribute(
      'href',
      '/devices?hardware_health_status=critical',
    );
    expect(screen.getByText('2 warn')).toBeInTheDocument();
  });

  it('maps critical tone to danger accent', () => {
    render(
      <AttentionListCard
        title="Attention"
        total={1}
        tone="critical"
        rows={[{ label: 'Telemetry coverage', values: '1 unsupported' }]}
      />,
    );

    expect(screen.getByText('Attention').closest('.border-l-danger-strong')).toBeInTheDocument();
  });
});
