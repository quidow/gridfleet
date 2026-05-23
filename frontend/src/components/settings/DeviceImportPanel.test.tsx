import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { DeviceImportPanel } from './DeviceImportPanel';

describe('DeviceImportPanel', () => {
  it('renders the upload step on mount', () => {
    render(
      <MemoryRouter>
        <DeviceImportPanel />
      </MemoryRouter>,
    );
    expect(screen.getByLabelText(/bundle/i)).toBeInTheDocument();
  });
});
