import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Sidebar from './Sidebar';

const mockUseAuth = vi.fn();
const mockUseSidebar = vi.fn();
const mockUseTheme = vi.fn();
const mockUseDevices = vi.fn();
const mockUseHosts = vi.fn();
const mockUseRuns = vi.fn();

vi.mock('../context/auth', () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock('../context/SidebarContext', () => ({
  useSidebar: () => mockUseSidebar(),
}));

vi.mock('../context/theme', () => ({
  useTheme: () => mockUseTheme(),
}));

vi.mock('../hooks/useDevices', () => ({
  useDevices: () => mockUseDevices(),
}));

vi.mock('../hooks/useHosts', () => ({
  useHosts: () => mockUseHosts(),
}));

vi.mock('../hooks/useRuns', () => ({
  useRuns: () => mockUseRuns(),
}));

describe('Sidebar', () => {
  beforeEach(() => {
    mockUseAuth.mockReturnValue({ enabled: false });
    mockUseSidebar.mockReturnValue({ collapsed: false, toggle: vi.fn() });
    mockUseTheme.mockReturnValue({ mode: 'light', toggle: vi.fn() });
    mockUseDevices.mockReturnValue({ data: [] });
    mockUseHosts.mockReturnValue({ data: [] });
    mockUseRuns.mockReturnValue({ data: { items: [] } });
  });

  it('orders fleet links before test run sessions', () => {
    render(
      <MemoryRouter>
        <Sidebar />
      </MemoryRouter>,
    );

    const labels = screen.getAllByRole('link').map((link) => link.textContent);

    expect(labels).toEqual([
      'Dashboard',
      'Devices0',
      'Device Groups',
      'Hosts0',
      'Drivers',
      'Test Runs',
      'Sessions',
      'Analytics',
      'Notifications',
      'Settings',
    ]);
  });
});
