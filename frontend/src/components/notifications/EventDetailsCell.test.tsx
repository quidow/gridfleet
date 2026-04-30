import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import EventDetailsCell from './EventDetailsCell';

describe('EventDetailsCell', () => {
  it('renders a registered event as sentence text', () => {
    render(<EventDetailsCell type="run.completed" data={{ name: 'live-run-00' }} />);
    expect(screen.getByText('live-run-00 completed')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders a registered circuit breaker event without regressions', () => {
    render(
      <EventDetailsCell
        type="host.circuit_breaker.opened"
        data={{ host: 'mac-1', consecutive_failures: 5, cooldown_seconds: 30 }}
      />,
    );
    expect(
      screen.getByText('Circuit breaker opened on mac-1 after 5 consecutive failure(s) (cooldown 30s)'),
    ).toBeInTheDocument();
  });

  it('renders a placeholder for unknown empty payloads', () => {
    render(<EventDetailsCell type="totally.unknown" data={{}} />);
    expect(screen.getByText('No details')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('renders collapsed raw JSON for unknown non-empty payloads', () => {
    render(<EventDetailsCell type="totally.unknown" data={{ foo: 'bar', count: 3 }} />);
    const button = screen.getByRole('button', { name: 'Show raw details' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('{"foo":"bar","count":3}')).toBeInTheDocument();
    expect(screen.getByText('{"foo":"bar","count":3}').tagName).toBe('CODE');
  });

  it('expands raw JSON when toggled', async () => {
    const user = userEvent.setup();
    render(<EventDetailsCell type="totally.unknown" data={{ foo: 'bar', count: 3 }} />);

    await user.click(screen.getByRole('button', { name: 'Show raw details' }));

    expect(screen.getByRole('button', { name: 'Hide raw details' })).toHaveAttribute('aria-expanded', 'true');
    const pre = screen.getByText((_content, node) => node?.tagName === 'PRE' && node.textContent !== null && node.textContent.includes('"foo": "bar"'));
    expect(pre.tagName).toBe('PRE');
  });

  it('does not render undefined artifacts for sparse registered payloads', () => {
    render(<EventDetailsCell type="run.failed" data={{}} />);
    expect(screen.getByText('Run failed')).toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });
});
