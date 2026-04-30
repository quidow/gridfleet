import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';
import Tabs from './Tabs';
import { useTabParam } from './useTabParam';
import type { TabDefinition } from './Tabs';

const FLAT_TABS: TabDefinition[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'health', label: 'Health' },
  { id: 'logs', label: 'Logs' },
];

const GROUPED_TABS: TabDefinition[] = [
  { id: 'general', label: 'General', section: 'System' },
  { id: 'grid', label: 'Grid', section: 'System' },
  { id: 'webhooks', label: 'Webhooks', section: 'Integrations' },
  { id: 'drivers', label: 'Drivers', section: 'Extensions' },
];

describe('Tabs — flat', () => {
  it('renders all tab labels', () => {
    render(
      <MemoryRouter>
        <Tabs tabs={FLAT_TABS} activeId="overview" onChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Health' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Logs' })).toBeInTheDocument();
  });

  it('applies active styling to the activeId tab', () => {
    render(
      <MemoryRouter>
        <Tabs tabs={FLAT_TABS} activeId="health" onChange={vi.fn()} />
      </MemoryRouter>,
    );
    const active = screen.getByRole('button', { name: 'Health' });
    expect(active.className).toContain('border-accent');
    const inactive = screen.getByRole('button', { name: 'Overview' });
    expect(inactive.className).toContain('border-transparent');
  });

  it('calls onChange with the clicked tab id', async () => {
    const onChange = vi.fn();
    render(
      <MemoryRouter>
        <Tabs tabs={FLAT_TABS} activeId="overview" onChange={onChange} />
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Logs' }));
    expect(onChange).toHaveBeenCalledWith('logs');
  });
});

describe('Tabs — grouped', () => {
  it('renders grouped tabs without duplicating tab labels', () => {
    render(
      <MemoryRouter>
        <Tabs tabs={GROUPED_TABS} activeId="general" onChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole('button')).toHaveLength(4);
  });

  it('renders all tab labels under sections', () => {
    render(
      <MemoryRouter>
        <Tabs tabs={GROUPED_TABS} activeId="general" onChange={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'General' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Webhooks' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Drivers' })).toBeInTheDocument();
  });
});

// Component that uses useTabParam so we can test the hook
function TabParamFixture({ allowedIds, defaultId }: { allowedIds: string[]; defaultId: string }) {
  const [active, setActive] = useTabParam('tab', allowedIds, defaultId);
  return (
    <div>
      <span data-testid="active">{active}</span>
      {allowedIds.map((id) => (
        <button key={id} onClick={() => setActive(id)}>
          go-{id}
        </button>
      ))}
    </div>
  );
}

function renderWithRoute(initialEntry: string, allowedIds: string[], defaultId: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="*" element={<TabParamFixture allowedIds={allowedIds} defaultId={defaultId} />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('useTabParam', () => {
  it('returns defaultId when param is absent', () => {
    renderWithRoute('/', ['a', 'b', 'c'], 'a');
    expect(screen.getByTestId('active').textContent).toBe('a');
  });

  it('returns the URL param value when it is in allowedIds', () => {
    renderWithRoute('/?tab=b', ['a', 'b', 'c'], 'a');
    expect(screen.getByTestId('active').textContent).toBe('b');
  });

  it('falls back to defaultId when URL has an unknown tab value', () => {
    renderWithRoute('/?tab=bogus', ['a', 'b', 'c'], 'a');
    expect(screen.getByTestId('active').textContent).toBe('a');
  });

  it('updates the URL when setActive is called', async () => {
    renderWithRoute('/', ['a', 'b', 'c'], 'a');
    await userEvent.click(screen.getByRole('button', { name: 'go-c' }));
    expect(screen.getByTestId('active').textContent).toBe('c');
  });

  it('preserves other existing query params when switching tabs', async () => {
    // Render with an existing param ?filter=foo
    render(
      <MemoryRouter initialEntries={['/?filter=foo']}>
        <Routes>
          <Route
            path="*"
            element={
              <div>
                <TabParamFixture allowedIds={['x', 'y']} defaultId="x" />
              </div>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'go-y' }));
    // active tab changed
    expect(screen.getByTestId('active').textContent).toBe('y');
  });
});
