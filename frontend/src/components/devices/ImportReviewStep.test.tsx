import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ImportReviewStep } from './ImportReviewStep';
import type { ImportPreview } from '../../api/devicesPortability';

const PREVIEW: ImportPreview = {
  schema_version: 1,
  exported_at: '2026-05-23T00:00:00Z',
  bundle_hash: 'sha256:abcd',
  available_hosts: [{ id: 'host-1', hostname: 'lab-04' }],
  rows: [
    {
      index: 0,
      device: { name: 'Pixel 7', original_host: { hostname: 'lab-04' } } as never,
      status: 'valid_new',
      host_suggestion: { id: 'host-1', hostname: 'lab-04' },
      issues: [],
    },
    {
      index: 1,
      device: { name: 'Pixel 8', original_host: null } as never,
      status: 'invalid',
      host_suggestion: null,
      issues: ['missing field'],
    },
  ],
};

describe('ImportReviewStep', () => {
  it('shows the step heading and status badges', () => {
    render(
      <ImportReviewStep
        preview={PREVIEW}
        mappings={{ 0: { target_host_id: '', included: true } }}
        onSetMapping={vi.fn()}
        onToggleIncluded={vi.fn()}
        onCommit={vi.fn()}
      />,
    );
    expect(screen.getByRole('heading', { name: /step 2.*review/i })).toBeInTheDocument();
    expect(screen.getByText('valid')).toBeInTheDocument();
    expect(screen.getByText('invalid')).toBeInTheDocument();
  });

  it('disables commit when included rows are missing a target host', () => {
    render(
      <ImportReviewStep
        preview={PREVIEW}
        mappings={{ 0: { target_host_id: '', included: true } }}
        onSetMapping={vi.fn()}
        onToggleIncluded={vi.fn()}
        onCommit={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: /commit import/i })).toBeDisabled();
  });

  it('enables commit and emits when all included rows have a host', () => {
    const onCommit = vi.fn();
    render(
      <ImportReviewStep
        preview={PREVIEW}
        mappings={{ 0: { target_host_id: 'host-1', included: true } }}
        onSetMapping={vi.fn()}
        onToggleIncluded={vi.fn()}
        onCommit={onCommit}
      />,
    );
    const commit = screen.getByRole('button', { name: /commit import/i });
    expect(commit).not.toBeDisabled();
    fireEvent.click(commit);
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  it('renders one row per preview entry', () => {
    render(
      <ImportReviewStep
        preview={PREVIEW}
        mappings={{ 0: { target_host_id: 'host-1', included: true } }}
        onSetMapping={vi.fn()}
        onToggleIncluded={vi.fn()}
        onCommit={vi.fn()}
      />,
    );
    const table = screen.getByRole('table');
    const bodyRows = within(table).getAllByRole('row').slice(1); // drop header
    expect(bodyRows).toHaveLength(2);
  });
});
