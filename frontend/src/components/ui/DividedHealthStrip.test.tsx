import { render, screen } from '@testing-library/react';
import { Database, Server } from 'lucide-react';
import { describe, expect, it } from 'vitest';
import DividedHealthStrip from './DividedHealthStrip';

describe('DividedHealthStrip', () => {
  it('renders cells with values and details', () => {
    render(
      <DividedHealthStrip
        cells={[
          { icon: Database, label: 'Database', tone: 'ok', value: 'Connected' },
          { icon: Server, label: 'Hosts', tone: 'warn', value: '1/2 online', detail: '1 offline' },
        ]}
      />,
    );

    expect(screen.getByText('Database')).toBeInTheDocument();
    expect(screen.getByText('Connected')).toBeInTheDocument();
    expect(screen.getByText('1 offline')).toBeInTheDocument();
  });

  it('maps error tone to danger token', () => {
    render(
      <DividedHealthStrip
        cells={[{ icon: Database, label: 'Database', tone: 'error', value: 'Disconnected' }]}
      />,
    );

    expect(screen.getByText('Disconnected').className).toMatch(/text-danger-foreground/);
  });

  it('maps neutral tone to gray text', () => {
    render(
      <DividedHealthStrip
        cells={[{ icon: Database, label: 'Database', tone: 'neutral', value: 'Unknown' }]}
      />,
    );

    expect(screen.getByText('Unknown').className).toMatch(/text-text-2/);
  });
});
