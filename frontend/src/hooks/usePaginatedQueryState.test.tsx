import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { usePaginatedQueryState } from './usePaginatedQueryState';

function QueryStateFixture() {
  const location = useLocation();
  const {
    page,
    pageSize,
    sort,
    direction,
    updateParams,
    setPage,
    setPageSize,
    setSort,
  } = usePaginatedQueryState({
    defaultPageSize: 50,
    allowedSortKeys: ['created_at', 'name'] as const,
    defaultSortKey: 'created_at',
    defaultSortDirection: 'desc',
  });

  return (
    <div>
      <div data-testid="page">{page}</div>
      <div data-testid="pageSize">{pageSize}</div>
      <div data-testid="sort">{sort}</div>
      <div data-testid="direction">{direction}</div>
      <div data-testid="search">{location.search}</div>
      <button onClick={() => updateParams({ state: 'active' }, { resetPage: true })}>set-filter</button>
      <button onClick={() => setPage(3)}>set-page</button>
      <button onClick={() => setPageSize(100)}>set-page-size</button>
      <button onClick={() => setSort('name', 'asc')}>set-sort</button>
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

describe('usePaginatedQueryState', () => {
  it('reads defaults when params are absent', () => {
    renderFixture();

    expect(screen.getByTestId('page').textContent).toBe('1');
    expect(screen.getByTestId('pageSize').textContent).toBe('50');
    expect(screen.getByTestId('sort').textContent).toBe('created_at');
    expect(screen.getByTestId('direction').textContent).toBe('desc');
  });

  it('reads existing query params when they are valid', () => {
    renderFixture('/?page=2&pageSize=25&sort=name&direction=asc');

    expect(screen.getByTestId('page').textContent).toBe('2');
    expect(screen.getByTestId('pageSize').textContent).toBe('25');
    expect(screen.getByTestId('sort').textContent).toBe('name');
    expect(screen.getByTestId('direction').textContent).toBe('asc');
  });

  it('resets page to 1 when filters change', async () => {
    renderFixture('/?page=4');

    await userEvent.click(screen.getByRole('button', { name: 'set-filter' }));

    expect(screen.getByTestId('search').textContent).toContain('state=active');
    expect(screen.getByTestId('search').textContent).toContain('page=1');
  });

  it('updates page and page size in the query string', async () => {
    renderFixture();

    await userEvent.click(screen.getByRole('button', { name: 'set-page' }));
    await userEvent.click(screen.getByRole('button', { name: 'set-page-size' }));

    expect(screen.getByTestId('search').textContent).toContain('pageSize=100');
    expect(screen.getByTestId('search').textContent).toContain('page=1');
  });

  it('updates sort and direction while preserving unrelated params', async () => {
    renderFixture('/?state=ready');

    await userEvent.click(screen.getByRole('button', { name: 'set-sort' }));

    expect(screen.getByTestId('search').textContent).toContain('state=ready');
    expect(screen.getByTestId('search').textContent).toContain('sort=name');
    expect(screen.getByTestId('search').textContent).toContain('direction=asc');
  });
});
