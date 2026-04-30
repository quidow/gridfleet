import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { UploadDriverPackForm } from './UploadDriverPackForm';

const mockUploadDriverPack = vi.fn();

vi.mock('../../api/driverPackAuthoring', () => ({
  createLocalDriverPack: vi.fn(),
  dryRunLocalDriverPack: vi.fn(),
  uploadDriverPack: (...args: unknown[]) => mockUploadDriverPack(...args),
}));

function makeFile(name = 'driver.tar.gz'): File {
  return new File(['tarball-bytes'], name, { type: 'application/gzip' });
}

function renderForm(onSuccess = vi.fn(), onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UploadDriverPackForm onSuccess={onSuccess} onClose={onClose} />
    </QueryClientProvider>,
  );
}

describe('UploadDriverPackForm', () => {
  beforeEach(() => {
    mockUploadDriverPack.mockReset();
  });

  it('renders file input accepting tarball extensions and gzip archive MIME types', () => {
    renderForm();
    const input = screen.getByLabelText(/driver tarball/i);
    expect(input).toHaveAttribute('type', 'file');
    expect(input).toHaveAttribute(
      'accept',
      '.tar.gz,.tgz,.tar,.gz,application/gzip,application/x-gzip,application/x-tar',
    );
  });

  it('renders the security confirmation checkbox', () => {
    renderForm();
    expect(
      screen.getByRole('checkbox', { name: /execute python code/i }),
    ).toBeInTheDocument();
  });

  it('submit button is disabled when no file is selected', () => {
    renderForm();
    expect(screen.getByRole('button', { name: /upload driver/i })).toBeDisabled();
  });

  it('submit button is disabled when file selected but checkbox unchecked', async () => {
    renderForm();
    const input = screen.getByLabelText(/driver tarball/i);
    await userEvent.upload(input, makeFile());
    expect(screen.getByRole('button', { name: /upload driver/i })).toBeDisabled();
  });

  it('submit button is disabled when checkbox checked but no file selected', async () => {
    renderForm();
    await userEvent.click(screen.getByRole('checkbox', { name: /execute python code/i }));
    expect(screen.getByRole('button', { name: /upload driver/i })).toBeDisabled();
  });

  it('submit button is enabled when file selected AND checkbox checked', async () => {
    renderForm();
    const input = screen.getByLabelText(/driver tarball/i);
    await userEvent.upload(input, makeFile());
    await userEvent.click(screen.getByRole('checkbox', { name: /execute python code/i }));
    expect(screen.getByRole('button', { name: /upload driver/i })).not.toBeDisabled();
  });

  it('calls uploadDriverPack with a FormData containing the file under "tarball" key', async () => {
    mockUploadDriverPack.mockResolvedValue({ id: 'vendor-foo', state: 'enabled' });
    renderForm();
    const file = makeFile();
    await userEvent.upload(screen.getByLabelText(/driver tarball/i), file);
    await userEvent.click(screen.getByRole('checkbox', { name: /execute python code/i }));
    await userEvent.click(screen.getByRole('button', { name: /upload driver/i }));
    await waitFor(() => expect(mockUploadDriverPack).toHaveBeenCalledOnce());
    const [passedFile] = mockUploadDriverPack.mock.calls[0] as [File];
    expect(passedFile).toBe(file);
  });

  it('calls onSuccess and onClose after successful upload', async () => {
    mockUploadDriverPack.mockResolvedValue({ id: 'vendor-foo', state: 'enabled' });
    const onSuccess = vi.fn();
    const onClose = vi.fn();
    renderForm(onSuccess, onClose);
    await userEvent.upload(screen.getByLabelText(/driver tarball/i), makeFile());
    await userEvent.click(screen.getByRole('checkbox', { name: /execute python code/i }));
    await userEvent.click(screen.getByRole('button', { name: /upload driver/i }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledOnce());
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('shows inline error message on failure', async () => {
    mockUploadDriverPack.mockRejectedValue(new Error('Server rejected the tarball'));
    renderForm();
    await userEvent.upload(screen.getByLabelText(/driver tarball/i), makeFile());
    await userEvent.click(screen.getByRole('checkbox', { name: /execute python code/i }));
    await userEvent.click(screen.getByRole('button', { name: /upload driver/i }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
