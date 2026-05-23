import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import BackupRestorePanel from './BackupRestorePanel';

describe('BackupRestorePanel', () => {
  function renderPanel() {
    return render(
      <MemoryRouter>
        <BackupRestorePanel />
      </MemoryRouter>,
    );
  }

  it('renders the panel title', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: /backup & restore/i })).toBeInTheDocument();
  });

  it('renders the export configuration section with its button', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: /export configuration/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /export config/i })).toBeInTheDocument();
  });

  it('renders the import devices section with the upload step', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: /import devices/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/bundle/i)).toBeInTheDocument();
  });
});
