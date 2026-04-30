import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import SummaryPill from './SummaryPill';

describe('SummaryPill', () => {
  it('renders label and value', () => {
    render(<SummaryPill tone="ok" label="Hosts" value="3/4" />);
    expect(screen.getByText('Hosts')).toBeInTheDocument();
    expect(screen.getByText('3/4')).toBeInTheDocument();
  });

  it('maps warn tone to warning token dot', () => {
    render(<SummaryPill tone="warn" label="Queued" />);
    expect(screen.getByText('Queued').previousElementSibling?.className).toMatch(/bg-warning-strong/);
  });

  it('renders only label when value missing', () => {
    render(<SummaryPill tone="neutral" label="DB" />);
    expect(screen.getByText('DB').parentElement).toHaveTextContent('DB');
  });
});
