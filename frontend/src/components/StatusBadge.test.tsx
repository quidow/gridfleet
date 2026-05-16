import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusBadge } from './StatusBadge';

describe('StatusBadge', () => {
  it('renders formatted status text by default', () => {
    render(<StatusBadge status="recovery_backoff" />);
    expect(screen.getByText('Recovery Backoff')).toBeInTheDocument();
  });

  it('uses an explicit label when provided', () => {
    render(<StatusBadge status="running" label="In progress" />);
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.queryByText('Running')).not.toBeInTheDocument();
  });
});
