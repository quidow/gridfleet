import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { useCursorQueryState } from './useCursorQueryState';

function QueryStateFixture() {
  const location = useLocation();
  const {
    pageSize,
    cursor,
    direction,
    updateParams,
    setPageSize,
    goOlder,
    goNewer,
    resetToNewest,
  } = useCursorQueryState({
    defaultPageSize: 50,
  });

  return (
    <div>
      <div data-testid="pageSize">{pageSize}</div>
      <div data-testid="cursor">{cursor}</div>
      <div data-testid="direction">{direction}</div>
      <div data-testid="search">{location.search}</div>
      <button onClick={() => updateParams({ state: 'active' }, { resetCursor: true })}>set-filter</button>
      <button onClick={() => setPageSize(100)}>set-page-size</button>
      <button onClick={() => goOlder('cursor-older')}>older</button>
      <button onClick={() => goNewer('cursor-newer')}>newer</button>
      <button onClick={resetToNewest}>reset</button>
    </div>
  );
}

function renderFixture(initialEntry = '/') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="*" element={<QueryStateFixture />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('useCursorQueryState', () => {
  it('reads defaults when params are absent', () => {
    renderFixture();

    expect(screen.getByTestId('pageSize').textContent).toBe('50');
    expect(screen.getByTestId('cursor').textContent).toBe('');
    expect(screen.getByTestId('direction').textContent).toBe('older');
  });

  it('reads existing cursor params when they are valid', () => {
    renderFixture('/?pageSize=25&cursor=cursor-1&cursorDirection=newer');

    expect(screen.getByTestId('pageSize').textContent).toBe('25');
    expect(screen.getByTestId('cursor').textContent).toBe('cursor-1');
    expect(screen.getByTestId('direction').textContent).toBe('newer');
  });

  it('resets cursor state when filters change', async () => {
    renderFixture('/?cursor=cursor-older&cursorDirection=older');

    await userEvent.click(screen.getByRole('button', { name: 'set-filter' }));

    expect(screen.getByTestId('search').textContent).toContain('state=active');
    expect(screen.getByTestId('search').textContent).not.toContain('cursor=');
    expect(screen.getByTestId('search').textContent).not.toContain('cursorDirection=');
  });

  it('updates cursor direction for history navigation', async () => {
    renderFixture();

    await userEvent.click(screen.getByRole('button', { name: 'older' }));
    expect(screen.getByTestId('search').textContent).toContain('cursor=cursor-older');
    expect(screen.getByTestId('search').textContent).toContain('cursorDirection=older');

    await userEvent.click(screen.getByRole('button', { name: 'newer' }));
    expect(screen.getByTestId('search').textContent).toContain('cursor=cursor-newer');
    expect(screen.getByTestId('search').textContent).toContain('cursorDirection=newer');
  });

  it('resets to newest when page size changes or reset is requested', async () => {
    renderFixture('/?cursor=cursor-older&cursorDirection=older');

    await userEvent.click(screen.getByRole('button', { name: 'set-page-size' }));
    expect(screen.getByTestId('search').textContent).toContain('pageSize=100');
    expect(screen.getByTestId('search').textContent).not.toContain('cursor=');

    await userEvent.click(screen.getByRole('button', { name: 'older' }));
    await userEvent.click(screen.getByRole('button', { name: 'reset' }));
    expect(screen.getByTestId('search').textContent).not.toContain('cursor=');
    expect(screen.getByTestId('search').textContent).not.toContain('cursorDirection=');
  });
});
