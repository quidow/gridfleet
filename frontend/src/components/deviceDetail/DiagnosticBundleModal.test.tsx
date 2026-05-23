import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { DiagnosticBundleModal } from './DiagnosticBundleModal';

function renderModal(props: Partial<ComponentProps<typeof DiagnosticBundleModal>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const merged = {
    open: true,
    title: 'Bundle',
    payload: { schema_version: 1, hello: 'world' },
    redacted: false,
    onClose: vi.fn(),
    onToggleRedact: vi.fn(),
    ...props,
  };
  return {
    user: userEvent.setup(),
    onClose: merged.onClose,
    onToggleRedact: merged.onToggleRedact,
    ...render(
      <QueryClientProvider client={queryClient}>
        <DiagnosticBundleModal {...merged} />
      </QueryClientProvider>,
    ),
  };
}

describe('DiagnosticBundleModal', () => {
  it('renders the JSON payload', () => {
    renderModal();
    expect(screen.getByText(/"schema_version": 1/)).toBeInTheDocument();
  });

  it('invokes onToggleRedact when the toggle is clicked', async () => {
    const { user, onToggleRedact } = renderModal();
    await user.click(screen.getByRole('button', { name: /redact/i }));
    expect(onToggleRedact).toHaveBeenCalled();
  });

  it('copies the payload to the clipboard', async () => {
    const { user } = renderModal();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    await user.click(screen.getByRole('button', { name: /copy/i }));
    expect(writeText).toHaveBeenCalled();
  });
});
