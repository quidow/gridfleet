import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import Layout from './Layout';

vi.mock('./Sidebar', () => ({
  default: () => <aside data-testid="sidebar" />,
}));

vi.mock('../hooks/useEventStream', () => ({
  useEventStream: () => ({ connected: false }),
}));

describe('Layout', () => {
  it('applies the shared page gutter to the route content shell', () => {
    const { container } = render(
      <MemoryRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<div>Route content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    const contentShell = container.querySelector('main > div');

    expect(contentShell?.className).toContain('page-gutter');
    expect(contentShell?.className).toContain('min-h-full');
  });
});
