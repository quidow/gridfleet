import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { Sessions } from './Sessions';
import type { SessionDetail } from '../types';

const hooks = vi.hoisted(() => ({
  useSessions: vi.fn(),
  useKillSession: vi.fn(),
  killMutate: vi.fn(),
}));

vi.mock('../hooks/useSessions', () => ({
  useSessions: hooks.useSessions,
  useKillSession: hooks.useKillSession,
}));
vi.mock('../hooks/useDevices', () => ({ useDevices: () => ({ data: [] }) }));
vi.mock('../hooks/useGridQueue', () => ({ useGridQueue: () => ({ data: { requests: [] } }) }));
vi.mock('../hooks/useDriverPacks', () => ({ useDriverPackCatalog: () => ({ data: [] }) }));
vi.mock('../hooks/usePageTitle', () => ({ usePageTitle: vi.fn() }));

const RUNNING: Partial<SessionDetail> = {
  id: 'row-1',
  session_id: 'sess-running-1',
  status: 'running',
  started_at: '2026-06-07T10:00:00Z',
  ended_at: null,
  device_id: 'dev-1',
  device_name: 'Pixel 8',
  requested_capabilities: { platformName: 'Android' },
  actual_capabilities: { 'appium:systemPort': 8201 },
  is_probe: false,
};

describe('Sessions', () => {
  beforeEach(() => {
    hooks.useSessions.mockReturnValue({
      data: { items: [RUNNING], limit: 200, next_cursor: null, prev_cursor: null },
      isLoading: false,
      dataUpdatedAt: Date.now(),
    });
    hooks.useKillSession.mockReturnValue({ mutate: hooks.killMutate, isPending: false });
    hooks.killMutate.mockClear();
  });

  it('defaults to the Active tab and lists running sessions', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <Sessions />
      </MemoryRouter>,
    );
    expect(screen.getByRole('button', { name: 'Active' })).toBeInTheDocument();
    expect(screen.getByText('Pixel 8')).toBeInTheDocument();
    expect(hooks.useSessions).toHaveBeenCalledWith(expect.objectContaining({ active: true }), 5_000);
  });

  it('expands a row to show capabilities', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <Sessions />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Expand capabilities' }));
    expect(screen.getByText('Requested capabilities')).toBeInTheDocument();
    expect(screen.getByText(/"appium:systemPort": 8201/)).toBeInTheDocument();
  });

  it('kill flow requires confirmation then fires the mutation', () => {
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <Sessions />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: /kill session sess-running-1/i }));
    expect(hooks.killMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Kill session' }));
    expect(hooks.killMutate).toHaveBeenCalledWith('sess-running-1', expect.anything());
  });

  it('renders the History tab when ?tab=history', () => {
    render(
      <MemoryRouter initialEntries={['/sessions?tab=history']}>
        <Sessions />
      </MemoryRouter>,
    );
    // History keeps the filter bar; Active has none.
    expect(screen.getByLabelText('Status')).toBeInTheDocument();
  });
});
