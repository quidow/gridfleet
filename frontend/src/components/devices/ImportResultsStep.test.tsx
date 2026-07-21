import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { ImportResultsStep } from './ImportResultsStep';
import type { ImportCommitResult } from '../../api/devicesPortability';

const RESULT: ImportCommitResult = {
  created: [{ index: 0, device_id: 'device-1' }],
  skipped: [{ index: 1, reason: 'conflict' }],
  failed: [{ index: 2, reason: 'invalid' }],
  memberships_skipped: [
    { index: 0, group_key: 'stale-group', reason: "static group 'stale-group' deleted during import" },
  ],
};

function renderResults(props?: Partial<{ result: ImportCommitResult; onReset: () => void }>) {
  return render(
    <MemoryRouter>
      <ImportResultsStep result={props?.result ?? RESULT} onReset={props?.onReset ?? vi.fn()} />
    </MemoryRouter>,
  );
}

describe('ImportResultsStep', () => {
  it('shows summary badges with each count', () => {
    renderResults();
    expect(screen.getByText(/1 created/i)).toBeInTheDocument();
    expect(screen.getByText(/1 skipped/i)).toBeInTheDocument();
    expect(screen.getByText(/1 failed/i)).toBeInTheDocument();
  });

  it('renders a row per created/skipped/failed/memberships-skipped entry', () => {
    renderResults();
    const tables = screen.getAllByRole('table');
    expect(tables).toHaveLength(4);
    tables.forEach((table) => {
      const bodyRows = within(table).getAllByRole('row').slice(1);
      expect(bodyRows).toHaveLength(1);
    });
  });

  it('renders a memberships-skipped section when memberships were skipped', () => {
    renderResults();
    expect(screen.getByText(/1 memberships skipped/i)).toBeInTheDocument();
    expect(screen.getByText('stale-group')).toBeInTheDocument();
    expect(
      screen.getByText(/static group 'stale-group' deleted during import/i),
    ).toBeInTheDocument();
  });

  it('hides the memberships-skipped section when none were skipped', () => {
    renderResults({
      result: { created: [], skipped: [], failed: [] },
    });
    expect(screen.queryByText(/memberships skipped/i)).not.toBeInTheDocument();
  });

  it('links created device ids to the device detail page', () => {
    renderResults();
    expect(screen.getByRole('link', { name: 'device-1' })).toHaveAttribute(
      'href',
      '/devices/device-1',
    );
  });

  it('fires onReset when the secondary button is clicked', () => {
    const onReset = vi.fn();
    renderResults({ onReset });
    fireEvent.click(screen.getByRole('button', { name: /import another bundle/i }));
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
