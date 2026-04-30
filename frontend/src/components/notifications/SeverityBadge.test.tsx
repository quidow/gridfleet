import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import SeverityBadge from './SeverityBadge';

describe('SeverityBadge', () => {
  it('derives danger tone and Critical label from event type', () => {
    render(<SeverityBadge eventType="run.failed" />);
    expect(screen.getByText('Critical').className).toMatch(/bg-danger-soft/);
  });

  it('derives warning tone from lifecycle incident events', () => {
    render(<SeverityBadge eventType="lifecycle.incident_open" />);
    expect(screen.getByText('Warning').className).toMatch(/bg-warning-soft/);
  });

  it('derives success tone from resolved lifecycle events', () => {
    render(<SeverityBadge eventType="lifecycle.incident_resolved" />);
    expect(screen.getByText('Success').className).toMatch(/bg-success-soft/);
  });

  it('falls back to neutral for unknown event types', () => {
    render(<SeverityBadge eventType="unknown.event" />);
    expect(screen.getByText('Neutral').className).toMatch(/bg-neutral-soft/);
  });
});
