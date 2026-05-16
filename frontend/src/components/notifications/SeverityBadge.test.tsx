import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import SeverityBadge from './SeverityBadge';

describe('SeverityBadge', () => {
  it('renders the event severity when present', () => {
    render(<SeverityBadge event={{ type: 'device.operational_state_changed', severity: 'success' }} />);
    expect(screen.getByText('Success').className).toMatch(/bg-success-soft/);
  });

  it('falls back to legacy map when severity is null', () => {
    render(<SeverityBadge event={{ type: 'node.crash', severity: null }} />);
    expect(screen.getByText('Critical').className).toMatch(/bg-danger-soft/);
  });

  it('falls back to legacy map when severity is absent', () => {
    render(<SeverityBadge event={{ type: 'lifecycle.incident_open' }} />);
    expect(screen.getByText('Warning').className).toMatch(/bg-warning-soft/);
  });

  it('falls back to neutral for unknown event types with no severity', () => {
    render(<SeverityBadge event={{ type: 'unknown.event' }} />);
    expect(screen.getByText('Neutral').className).toMatch(/bg-neutral-soft/);
  });
});
