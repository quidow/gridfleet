// frontend/src/components/ui/Sparkline.test.tsx
import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import Sparkline from './Sparkline';

describe('Sparkline', () => {
  it('renders an svg with a polyline path derived from values', () => {
    const { container } = render(<Sparkline values={[1, 2, 3, 4, 5]} width={60} height={16} />);
    const svg = container.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute('viewBox')).toBe('0 0 60 16');
    const path = container.querySelector('path');
    expect(path).not.toBeNull();
    expect(path?.getAttribute('d')).toMatch(/^M/);
  });

  it('renders nothing when values is empty', () => {
    const { container } = render(<Sparkline values={[]} />);
    expect(container.querySelector('path')).toBeNull();
  });

  it('renders nothing when only a single value is supplied', () => {
    const { container } = render(<Sparkline values={[7]} />);
    expect(container.querySelector('path')).toBeNull();
  });

  it('renders flat line when all values equal', () => {
    const { container } = render(<Sparkline values={[3, 3, 3, 3]} width={40} height={10} />);
    const path = container.querySelector('path');
    expect(path?.getAttribute('d')).toBe('M 0 5 H 40');
  });

  it('applies provided stroke class', () => {
    const { container } = render(<Sparkline values={[1, 2]} className="text-accent" />);
    const svg = container.querySelector('svg');
    expect(svg?.getAttribute('class')).toContain('text-accent');
  });

  it('includes aria-label when provided', () => {
    const { container } = render(<Sparkline values={[1, 2]} ariaLabel="Sessions last 7 days" />);
    const svg = container.querySelector('svg');
    expect(svg?.getAttribute('aria-label')).toBe('Sessions last 7 days');
  });
});

describe('Sparkline default height', () => {
  it('defaults to 28px height', () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} />);
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('height')).toBe('28');
    expect(svg.getAttribute('viewBox')).toBe('0 0 64 28');
  });

  it('still respects explicit height prop', () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} height={16} />);
    expect(container.querySelector('svg')!.getAttribute('height')).toBe('16');
  });
});

describe('Sparkline fill', () => {
  it('renders a filled closed path when fillClassName is provided', () => {
    const { container } = render(
      <Sparkline values={[1, 3, 2]} fillClassName="text-success-strong" />,
    );
    const paths = container.querySelectorAll('svg path');
    expect(paths.length).toBe(2);
    const fillPath = paths[0];
    expect(fillPath.getAttribute('fill')).toBe('currentColor');
    expect(fillPath.getAttribute('class') ?? '').toContain('text-success-strong');
  });

  it('does not render a fill path when fillClassName is absent', () => {
    const { container } = render(<Sparkline values={[1, 3, 2]} />);
    const paths = container.querySelectorAll('svg path');
    expect(paths.length).toBe(1);
  });

  it('renders fill path with fill-opacity 0.15', () => {
    const { container } = render(
      <Sparkline values={[1, 3, 2]} fillClassName="text-success-strong" />,
    );
    const fillPath = container.querySelector('svg path[fill="currentColor"]');
    expect(fillPath).toBeInTheDocument();
    expect(fillPath!.getAttribute('fill-opacity')).toBe('0.15');
  });

  it('falls back to className when fillClassName is omitted', () => {
    const { container } = render(
      <Sparkline values={[1, 3, 2]} className="text-success-strong" />,
    );
    const fillPath = container.querySelector('svg path[fill="currentColor"]');
    expect(fillPath).toBeInTheDocument();
    expect(fillPath!.getAttribute('class') ?? '').toContain('success-strong');
    expect(fillPath!.getAttribute('fill-opacity')).toBe('0.15');
  });
});
