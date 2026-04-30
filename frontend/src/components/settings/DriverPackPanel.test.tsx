import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import DriverPackPanel from './DriverPackPanel';

vi.mock('../../hooks/useDriverPacks', () => ({
  useDriverPackCatalog: () => ({
    data: [
      {
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        state: 'enabled',
      },
    ],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useSetDriverPackState: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe('DriverPackPanel', () => {
  it('renders link to drivers page', () => {
    render(
      <MemoryRouter>
        <DriverPackPanel />
      </MemoryRouter>,
    );

    const link = screen.getByRole('link', { name: /view all driver packs/i });
    expect(link).toHaveAttribute('href', '/drivers');
  });
});
