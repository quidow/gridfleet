import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { Notifications } from './Notifications';
import { fetchNotifications } from '../api/notifications';
import { useEventCatalog } from '../hooks/useEventCatalog';

vi.mock('../api/notifications', () => ({
  fetchNotifications: vi.fn(async () => ({ items: [], total: 0, limit: 25, offset: 0 })),
}));
vi.mock('../hooks/useEventCatalog', () => ({
  useEventCatalog: vi.fn(() => ({ data: [], isLoading: false })),
}));

function LocationProbe({ onLocation }: { onLocation: (path: string) => void }) {
  const loc = useLocation();
  onLocation(`${loc.pathname}${loc.search}`);
  return null;
}

function renderPage(initialPath = '/notifications') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const seen: string[] = [];
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route
            path="/notifications"
            element={
              <>
                <Notifications />
                <LocationProbe onLocation={(p) => seen.push(p)} />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { seen };
}

describe('Notifications severity filter', () => {
  beforeEach(() => {
    vi.mocked(fetchNotifications).mockClear();
    vi.mocked(useEventCatalog).mockReturnValue({ data: [], isLoading: false } as never);
  });

  it('renders severity filter popover with five checkboxes', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Severity filter' }));
    for (const label of ['Info', 'Success', 'Warning', 'Critical', 'Neutral']) {
      expect(screen.getByRole('checkbox', { name: label })).toBeInTheDocument();
    }
  });

  it('unchecking a severity filters to the rest', async () => {
    const { seen } = renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Severity filter' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Critical' }));
    await waitFor(() => {
      expect(seen.some((p) => p.includes('severity='))).toBe(true);
    });
    await waitFor(() => {
      const lastCall = vi.mocked(fetchNotifications).mock.calls.at(-1)?.[0];
      expect(lastCall?.severities).toEqual(['info', 'success', 'warning', 'neutral']);
    });
  });

  it('re-checking all severities clears the filter', async () => {
    const { seen } = renderPage('/notifications?severity=critical');
    await userEvent.click(screen.getByRole('button', { name: 'Severity filter' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Info' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Success' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Warning' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Neutral' }));
    await waitFor(() => {
      const last = seen.at(-1) ?? '';
      expect(last.includes('severity=')).toBe(false);
    });
  });

  it('unchecking two leaves three', async () => {
    const { seen } = renderPage();
    await userEvent.click(screen.getByRole('button', { name: 'Severity filter' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Warning' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Critical' }));
    await waitFor(() => {
      const last = seen.at(-1) ?? '';
      expect(last).toMatch(/severity=info%2Csuccess%2Cneutral|severity=info,success,neutral/);
    });
  });

  it('clear button clears type and severity', async () => {
    const { seen } = renderPage('/notifications?type=node.crash&severity=critical');
    await userEvent.click(screen.getByRole('button', { name: /clear/i }));
    await waitFor(() => {
      const last = seen.at(-1) ?? '';
      expect(last.includes('severity=')).toBe(false);
      expect(last.includes('type=')).toBe(false);
    });
  });
});
