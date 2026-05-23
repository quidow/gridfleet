import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ImportUploadStep } from './ImportUploadStep';

describe('ImportUploadStep', () => {
  it('shows the step heading and file input', () => {
    render(<ImportUploadStep onBundle={vi.fn()} />);
    expect(screen.getByRole('heading', { name: /step 1.*upload bundle/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/bundle/i)).toBeInTheDocument();
  });

  it('calls onBundle with parsed JSON when a valid bundle is uploaded', async () => {
    const onBundle = vi.fn();
    render(<ImportUploadStep onBundle={onBundle} />);
    const input = screen.getByLabelText(/bundle/i) as HTMLInputElement;
    const bundle = { schema_version: 1, devices: [] };
    const file = new File([JSON.stringify(bundle)], 'bundle.json', { type: 'application/json' });
    Object.defineProperty(input, 'files', { value: [file] });
    fireEvent.change(input);
    await new Promise((r) => setTimeout(r, 0));
    expect(onBundle).toHaveBeenCalledWith(expect.objectContaining({ schema_version: 1 }));
  });

  it('shows an inline error for an unsupported schema_version', async () => {
    render(<ImportUploadStep onBundle={vi.fn()} />);
    const input = screen.getByLabelText(/bundle/i) as HTMLInputElement;
    const file = new File([JSON.stringify({ schema_version: 99 })], 'bundle.json', { type: 'application/json' });
    Object.defineProperty(input, 'files', { value: [file] });
    fireEvent.change(input);
    expect(await screen.findByText(/unsupported schema_version: 99/)).toBeInTheDocument();
  });
});
