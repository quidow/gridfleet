import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PageHeader from './PageHeader';

describe('PageHeader', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-16T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders title and summary slot', () => {
    render(<PageHeader title="Dashboard" summary={<span>DB</span>} />);
    expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
    expect(screen.getByText('DB')).toBeInTheDocument();
  });

  it('combines subtitle and relative updated text', () => {
    render(<PageHeader title="Dashboard" subtitle="Fleet status" updatedAt={Date.now() - 45_000} />);
    expect(screen.getByText('Fleet status · updated 45s ago')).toBeInTheDocument();
  });

  it('renders standalone updated text without subtitle', () => {
    render(<PageHeader title="Devices" updatedAt={Date.now() - 5_000} />);
    expect(screen.getByText('Updated just now')).toBeInTheDocument();
  });

  it('renders actions slot', () => {
    render(<PageHeader title="Devices" actions={<button type="button">Add Device</button>} />);
    expect(screen.getByRole('button', { name: 'Add Device' })).toBeInTheDocument();
  });

  it('renders a ReactNode subtitle verbatim when no updatedAt is provided', () => {
    render(
      <PageHeader
        title="Device"
        subtitle={
          <span data-testid="subtitle-node">
            <span>Android · API 37 · host-01</span>
            <span data-testid="inline-chip">chip</span>
          </span>
        }
      />,
    );
    expect(screen.getByTestId('subtitle-node')).toBeInTheDocument();
    expect(screen.getByText('Android · API 37 · host-01')).toBeInTheDocument();
    expect(screen.getByTestId('inline-chip')).toBeInTheDocument();
    expect(screen.queryByText(/updated/)).not.toBeInTheDocument();
  });

  it('appends updated suffix to a ReactNode subtitle as a sibling span', () => {
    render(
      <PageHeader
        title="Device"
        subtitle={<span data-testid="subtitle-node">meta</span>}
        updatedAt={Date.now() - 45_000}
      />,
    );
    expect(screen.getByTestId('subtitle-node')).toBeInTheDocument();
    expect(screen.getByText(/updated 45s ago/)).toBeInTheDocument();
  });

  it('combines string subtitle and relative updated text unchanged after the widening', () => {
    render(<PageHeader title="Device" subtitle="Fleet status" updatedAt={Date.now() - 45_000} />);
    expect(screen.getByText('Fleet status · updated 45s ago')).toBeInTheDocument();
  });
});
