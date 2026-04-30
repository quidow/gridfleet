import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import DeviceStatStrip from './DeviceStatStrip';

describe('DeviceStatStrip', () => {
  const now = new Date('2026-04-20T12:00:00Z');

  it('renders four card labels', () => {
    render(
      <DeviceStatStrip
        summary={{
          sessions24h: null,
          passRate7d: null,
          failures7d: null,
          lastSession: null,
        }}
        now={now}
      />,
    );
    expect(screen.getByText('Sessions 24h')).toBeInTheDocument();
    expect(screen.getByText('Pass rate 7d')).toBeInTheDocument();
    expect(screen.getByText('Failures 7d')).toBeInTheDocument();
    expect(screen.getByText('Last session')).toBeInTheDocument();
  });

  it('renders em-dash for missing sessions count', () => {
    render(
      <DeviceStatStrip
        summary={{
          sessions24h: null,
          passRate7d: null,
          failures7d: null,
          lastSession: null,
        }}
        now={now}
      />,
    );
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3);
  });

  it('renders 0 literally when sessions24h is 0', () => {
    render(
      <DeviceStatStrip
        summary={{
          sessions24h: 0,
          passRate7d: null,
          failures7d: 0,
          lastSession: null,
        }}
        now={now}
      />,
    );
    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBe(2);
  });

  it('renders populated values from summary', () => {
    render(
      <DeviceStatStrip
        summary={{
          sessions24h: 2,
          passRate7d: 67,
          failures7d: 1,
          lastSession: '2026-04-20T11:00:00Z',
        }}
        now={now}
      />,
    );
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('67%')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('renders skeletons when loading', () => {
    render(
      <DeviceStatStrip
        summary={{
          sessions24h: null,
          passRate7d: null,
          failures7d: null,
          lastSession: null,
        }}
        isLoading
        now={now}
      />,
    );
    expect(screen.getAllByTestId('device-stat-skeleton').length).toBe(4);
  });
});
