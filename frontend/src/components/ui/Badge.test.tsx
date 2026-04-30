import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import Badge from './Badge';

describe('Badge', () => {
  it('renders children', () => {
    render(<Badge>online</Badge>);
    expect(screen.getByText('online')).toBeInTheDocument();
  });

  it('neutral tone maps to neutral tokens', () => {
    render(<Badge tone="neutral">neutral</Badge>);
    expect(screen.getByText('neutral').className).toMatch(/bg-neutral-soft/);
  });

  it('info tone maps to info tokens', () => {
    render(<Badge tone="info">info</Badge>);
    const el = screen.getByText('info');
    expect(el.className).toMatch(/bg-info-soft/);
  });

  it('success tone maps to success tokens', () => {
    render(<Badge tone="success">ok</Badge>);
    expect(screen.getByText('ok').className).toMatch(/bg-success-soft/);
  });

  it('warning tone maps to warning tokens', () => {
    render(<Badge tone="warning">warn</Badge>);
    expect(screen.getByText('warn').className).toMatch(/bg-warning-soft/);
  });

  it('danger tone maps to danger tokens', () => {
    render(<Badge tone="danger">err</Badge>);
    expect(screen.getByText('err').className).toMatch(/bg-danger-soft/);
  });

  it('renders a dot when dot=true', () => {
    render(<Badge dot>with-dot</Badge>);
    // The dot is a span sibling of the text
    const container = screen.getByText('with-dot');
    expect(container.parentElement?.querySelector('[aria-hidden]')).toBeInTheDocument();
  });
});
