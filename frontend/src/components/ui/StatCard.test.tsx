import { render, screen } from '@testing-library/react';
import { Activity, Server, Smartphone } from 'lucide-react';
import { describe, expect, it } from 'vitest';
import StatCard from './StatCard';

describe('StatCard', () => {
  it('renders label, value, and hint', () => {
    render(<StatCard label="Hosts" value={4} icon={Server} hint="3 online" />);
    expect(screen.getByText('Hosts')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('3 online')).toBeInTheDocument();
  });

  it('maps critical tone to danger border and danger icon tint', () => {
    const { container } = render(
      <StatCard label="Offline" value={2} icon={Server} tone="critical" />,
    );
    expect(screen.getByText('Offline').closest('.border-l-danger-strong')).toBeInTheDocument();
    expect(container.querySelector('.bg-danger-soft.text-danger-foreground')).not.toBeNull();
  });

  it('maps warn tone to warning border and warning icon tint', () => {
    const { container } = render(
      <StatCard label="Offline" value={1} icon={Server} tone="warn" />,
    );
    expect(screen.getByText('Offline').closest('.border-l-warning-strong')).toBeInTheDocument();
    expect(container.querySelector('.bg-warning-soft.text-warning-foreground')).not.toBeNull();
  });

  it('defaults neutral tone to border-l-border and neutral icon tint', () => {
    const { container } = render(<StatCard label="Hosts" value={4} icon={Server} />);
    expect(screen.getByText('Hosts').closest('.border-l-border')).toBeInTheDocument();
    expect(container.querySelector('.bg-neutral-soft.text-neutral-foreground')).not.toBeNull();
  });

  it('applies hover-lift utility on the outer card', () => {
    render(<StatCard label="Hosts" value={4} icon={Server} />);
    expect(screen.getByText('Hosts').closest('.hover-lift')).toBeInTheDocument();
  });

  it('renders a sparkline when `sparkline` prop has at least 2 values', () => {
    const { container } = render(
      <StatCard label="Sessions" value={3} icon={Activity} sparkline={[1, 2, 5, 3, 7]} />,
    );
    expect(container.querySelector('svg path[d]')).not.toBeNull();
  });

  it('does not render a sparkline when `sparkline` prop is absent', () => {
    const { container } = render(<StatCard label="Hosts" value={1} icon={Server} />);
    expect(container.querySelector('svg path[d]')).toBeNull();
  });

  it('does not render a sparkline when `sparkline` has fewer than 2 values', () => {
    const { container } = render(<StatCard label="Hosts" value={1} icon={Server} sparkline={[7]} />);
    expect(container.querySelector('svg path[d]')).toBeNull();
  });

  it('does not render a sparkline when all values are equal (flat series)', () => {
    const { container } = render(
      <StatCard label="Hosts" value={1} icon={Server} sparkline={[0, 0, 0, 0]} />,
    );
    expect(container.querySelector('svg path[d]')).toBeNull();
  });
});

describe('StatCard sparkline fill by tone', () => {
  it('passes a tone-specific fillClassName to Sparkline', () => {
    const { container } = render(
      <StatCard label="Devices" value={3} icon={Smartphone} tone="warn" sparkline={[1, 2, 3, 2, 4]} />,
    );
    const fillPath = container.querySelector('svg path[fill="currentColor"]');
    expect(fillPath).toBeInTheDocument();
    expect(fillPath!.getAttribute('class') ?? '').toContain('warning-strong');
  });
});

describe('StatCard prop surface', () => {
  it('rejects a non-numeric sparkline at compile time', () => {
    // @ts-expect-error sparkline must be number[], not ReactElement.
    const rejected = <StatCard label="X" value={1} icon={Server} sparkline={<span />} />;
    // Real guard is the @ts-expect-error directive above; this JS assert just keeps vitest happy.
    expect(rejected).toBeTruthy();
  });

  it('rejects the removed accent prop at compile time', () => {
    // @ts-expect-error StatCard no longer accepts `accent`.
    const rejected = <StatCard label="X" value={1} icon={Server} accent="bg-accent-soft text-accent" />;
    // Real guard is the @ts-expect-error directive above; this JS assert just keeps vitest happy.
    expect(rejected).toBeTruthy();
  });
});
