import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import RunProgressBar from './RunProgressBar';

describe('RunProgressBar', () => {
  it('shows mono caption with non-zero segments only', () => {
    render(<RunProgressBar counts={{ passed: 12, failed: 1, error: 0, running: 3, total: 16 }} />);
    expect(screen.getByText(/12 pass/)).toBeInTheDocument();
    expect(screen.getByText(/1 fail/)).toBeInTheDocument();
    expect(screen.getByText(/3 running/)).toBeInTheDocument();
  });

  it('treats failed+error as a single danger segment in caption', () => {
    render(<RunProgressBar counts={{ passed: 0, failed: 2, error: 3, running: 0, total: 5 }} />);
    // caption shows combined fail count
    expect(screen.getByText(/5 fail/)).toBeInTheDocument();
  });

  it('renders empty-state caption when total is zero', () => {
    render(<RunProgressBar counts={{ passed: 0, failed: 0, error: 0, running: 0, total: 0 }} />);
    expect(screen.getByText(/no sessions yet/i)).toBeInTheDocument();
  });

  it('exposes accessible label summarising counts', () => {
    render(<RunProgressBar counts={{ passed: 10, failed: 0, error: 1, running: 0, total: 11 }} />);
    expect(screen.getByRole('img', { name: /10 pass.*1 fail/i })).toBeInTheDocument();
  });
});
