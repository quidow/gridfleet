import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FetchError from './FetchError';

describe('FetchError', () => {
  it('renders the default message', () => {
    render(<FetchError onRetry={vi.fn()} />);
    expect(screen.getByRole('alert')).toBeDefined();
    expect(screen.getByText('Something went wrong while loading this data.')).toBeDefined();
  });

  it('renders a custom message', () => {
    render(<FetchError message="Could not load sessions." onRetry={vi.fn()} />);
    expect(screen.getByText('Could not load sessions.')).toBeDefined();
  });

  it('shows a Retry button', () => {
    render(<FetchError onRetry={vi.fn()} />);
    expect(screen.getByRole('button', { name: /retry/i })).toBeDefined();
  });

  it('calls onRetry when the Retry button is clicked', async () => {
    const onRetry = vi.fn();
    render(<FetchError onRetry={onRetry} />);
    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('forwards className to the container', () => {
    const { container } = render(<FetchError onRetry={vi.fn()} className="my-custom-class" />);
    expect(container.firstChild).toBeDefined();
    expect((container.firstChild as HTMLElement).classList.contains('my-custom-class')).toBe(true);
  });
});
