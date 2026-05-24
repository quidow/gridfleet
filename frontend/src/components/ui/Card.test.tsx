import { render, screen } from '@testing-library/react';
import { Card } from './Card';

test('renders children', () => {
  render(<Card>Card content</Card>);
  expect(screen.getByText('Card content')).toBeInTheDocument();
});

test('applies base card classes', () => {
  const { container } = render(<Card>content</Card>);
  const el = container.firstElementChild!;
  expect(el.className).toMatch(/rounded-lg/);
  expect(el.className).toMatch(/border-border/);
  expect(el.className).toMatch(/bg-surface-1/);
  expect(el.className).toMatch(/shadow-sm/);
});

test('defaults to md padding (p-4)', () => {
  const { container } = render(<Card>content</Card>);
  expect(container.firstElementChild!.className).toMatch(/p-4/);
});

test('padding="none" omits padding class', () => {
  const { container } = render(<Card padding="none">content</Card>);
  const className = container.firstElementChild!.className;
  expect(className).not.toMatch(/\bp-\d/);
});

test('padding="sm" applies p-3', () => {
  const { container } = render(<Card padding="sm">content</Card>);
  expect(container.firstElementChild!.className).toMatch(/p-3/);
});

test('padding="lg" applies p-6', () => {
  const { container } = render(<Card padding="lg">content</Card>);
  expect(container.firstElementChild!.className).toMatch(/p-6/);
});

test('renders as section when as="section"', () => {
  const { container } = render(<Card as="section">content</Card>);
  expect(container.firstElementChild!.tagName).toBe('SECTION');
});

test('renders as article when as="article"', () => {
  const { container } = render(<Card as="article">content</Card>);
  expect(container.firstElementChild!.tagName).toBe('ARTICLE');
});

test('defaults to div tag', () => {
  const { container } = render(<Card>content</Card>);
  expect(container.firstElementChild!.tagName).toBe('DIV');
});

test('passes className through', () => {
  const { container } = render(<Card className="custom-class">content</Card>);
  expect(container.firstElementChild!.className).toMatch(/custom-class/);
});
