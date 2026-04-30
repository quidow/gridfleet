import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi } from 'vitest';

import { AddDriverDialog } from './AddDriverDialog';

vi.mock('../../api/driverPackAuthoring', () => ({
  uploadDriverPack: vi.fn().mockResolvedValue({ id: 'vendor-foo', state: 'enabled' }),
}));

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AddDriverDialog isOpen={true} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe('AddDriverDialog', () => {
  it('does not render template picker', () => {
    renderDialog();
    expect(screen.queryByText('Choose a driver or template')).not.toBeInTheDocument();
    expect(screen.queryByText('Use this template')).not.toBeInTheDocument();
  });

  it('renders upload form directly', () => {
    renderDialog();
    expect(screen.getByRole('dialog', { name: /upload driver pack/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/driver tarball/i)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /execute python code/i })).toBeInTheDocument();
  });
});
