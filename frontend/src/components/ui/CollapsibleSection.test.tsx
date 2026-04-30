import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CollapsibleSection from './CollapsibleSection';

describe('CollapsibleSection', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('renders the title', () => {
    render(<CollapsibleSection title="My Section">body</CollapsibleSection>);
    expect(screen.getByText('My Section')).toBeDefined();
  });

  it('is collapsed by default when defaultOpen is not set', () => {
    render(<CollapsibleSection title="Collapsed">inner content</CollapsibleSection>);
    const button = screen.getByRole('button');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    // Body div is hidden
    const body = document.getElementById(button.getAttribute('aria-controls')!);
    expect(body?.hasAttribute('hidden')).toBe(true);
  });

  it('is open when defaultOpen is true', () => {
    render(<CollapsibleSection title="Open" defaultOpen>inner content</CollapsibleSection>);
    const button = screen.getByRole('button');
    expect(button.getAttribute('aria-expanded')).toBe('true');
    const body = document.getElementById(button.getAttribute('aria-controls')!);
    expect(body?.hasAttribute('hidden')).toBe(false);
  });

  it('toggles open/closed on click and updates aria-expanded', async () => {
    render(<CollapsibleSection title="Toggle">content</CollapsibleSection>);
    const button = screen.getByRole('button');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(button);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    await userEvent.click(button);
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  it('always renders the summary slot regardless of open state', () => {
    render(
      <CollapsibleSection title="With Summary" summary={<span>status pill</span>}>
        body
      </CollapsibleSection>,
    );
    expect(screen.getByText('status pill')).toBeDefined();
    // Summary visible while collapsed
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
  });

  it('persists open state to localStorage when persistKey is provided', async () => {
    render(
      <CollapsibleSection title="Persisted" persistKey="test.section.open">
        content
      </CollapsibleSection>,
    );
    const button = screen.getByRole('button');
    await userEvent.click(button);
    expect(JSON.parse(localStorage.getItem('test.section.open')!)).toBe(true);
  });

  it('reads initial open state from localStorage when persistKey matches', () => {
    localStorage.setItem('test.section.open', JSON.stringify(true));
    render(
      <CollapsibleSection title="Already Open" persistKey="test.section.open">
        content
      </CollapsibleSection>,
    );
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
  });

  it('falls back to defaultOpen when persisted value is corrupt JSON', () => {
    localStorage.setItem('test.section.open', 'bad{{json');
    render(
      <CollapsibleSection title="Corrupt" persistKey="test.section.open" defaultOpen={false}>
        content
      </CollapsibleSection>,
    );
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('false');
  });
});
