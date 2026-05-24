import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';

import { AddDriverDialog } from './AddDriverDialog';

vi.mock('../../api/driverPackAuthoring', () => ({
  uploadDriverPack: vi.fn().mockResolvedValue({ id: 'vendor-foo', state: 'enabled' }),
  fetchTemplates: vi.fn().mockResolvedValue([]),
  createFromTemplate: vi.fn(),
  forkDriverPack: vi.fn(),
}));

vi.mock('../../api/driverPacks', () => ({
  fetchDriverPackCatalog: vi.fn().mockResolvedValue([]),
  fetchHostDriverPacks: vi.fn(),
  setDriverPackState: vi.fn(),
}));

vi.mock('../../context/EventStreamContext', () => ({
  useEventStreamStatus: () => ({ connected: false }),
}));

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AddDriverDialog isOpen={true} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AddDriverDialog', () => {
  it('renders with "Add Driver Pack" title', () => {
    renderDialog();
    expect(screen.getByRole('dialog', { name: /add driver pack/i })).toBeInTheDocument();
  });

  it('renders three tabs', () => {
    renderDialog();
    expect(screen.getByText('From Template')).toBeInTheDocument();
    expect(screen.getByText('Upload Tarball')).toBeInTheDocument();
    expect(screen.getByText('Fork Existing')).toBeInTheDocument();
  });

  it('defaults to the From Template tab', () => {
    renderDialog();
    expect(screen.queryByLabelText(/driver tarball/i)).not.toBeInTheDocument();
  });

  it('switches to upload tab and shows file input', async () => {
    renderDialog();
    await userEvent.click(screen.getByText('Upload Tarball'));
    expect(screen.getByLabelText(/driver tarball/i)).toBeInTheDocument();
  });
});
