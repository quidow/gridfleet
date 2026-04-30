import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import BulkActionToolbar from './BulkActionToolbar';

function wrap(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('BulkActionToolbar', () => {
  const devices = [
    { id: 'a', name: 'Alpha', platform_id: 'android_mobile' },
    { id: 'b', name: 'Bravo', platform_id: 'android_mobile' },
  ] as never;

  it('renders count and Start/Stop/Delete buttons', () => {
    wrap(
      <BulkActionToolbar
        selectedIds={new Set(['a', 'b'])}
        selectedDevices={devices}
        onClearSelection={() => {}}
      />,
    );

    expect(screen.getByText('2 selected')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Start$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Stop$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Delete$/ })).toBeInTheDocument();
  });

  it('Auto-Manage popover closes on Escape', async () => {
    wrap(
      <BulkActionToolbar
        selectedIds={new Set(['a'])}
        selectedDevices={devices}
        onClearSelection={() => {}}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Auto-Manage/ }));

    expect(await screen.findByRole('menuitem', { name: 'Enable' })).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('menuitem', { name: 'Enable' })).toBeNull();
  });

  it('closes Auto-Manage menu when focus leaves the menu area', async () => {
    wrap(
      <BulkActionToolbar
        selectedIds={new Set(['a'])}
        selectedDevices={devices}
        onClearSelection={() => {}}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Auto-Manage/ }));
    expect(await screen.findByRole('menuitem', { name: 'Enable' })).toBeInTheDocument();

    await userEvent.tab();
    await userEvent.tab();

    expect(screen.queryByRole('menuitem', { name: 'Enable' })).toBeNull();
  });

  it('Escape clears the selection when no menu is open', async () => {
    const onClearSelection = vi.fn();
    wrap(
      <BulkActionToolbar
        selectedIds={new Set(['a'])}
        selectedDevices={devices}
        onClearSelection={onClearSelection}
      />,
    );

    screen.getByRole('button', { name: /^Start$/ }).focus();
    await userEvent.keyboard('{Escape}');

    expect(onClearSelection).toHaveBeenCalledTimes(1);
  });

  it('does not clear selection when Escape closes an open ConfirmDialog', async () => {
    const onClearSelection = vi.fn();
    wrap(
      <BulkActionToolbar
        selectedIds={new Set(['a'])}
        selectedDevices={devices}
        onClearSelection={onClearSelection}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /^Start$/ }));
    // ConfirmDialog (via Modal) renders role="dialog" with aria-modal="true"
    expect(await screen.findByRole('dialog', { name: 'Start Nodes' })).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');

    expect(onClearSelection).not.toHaveBeenCalled();
  });
});
