import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import DeviceConfigEditor from './DeviceConfigEditor';
import * as api from '../../api/devices';
import * as driverPacksApi from '../../api/driverPacks';
import { AuthContext, type AuthContextValue, DEFAULT_SESSION } from '../../context/auth';

vi.mock('../../api/devices');
vi.mock('../../api/driverPacks');
vi.mock('@monaco-editor/react', () => ({
  default: ({ value, onChange }: any) => (
    <textarea data-testid="monaco" value={value} onChange={(e: any) => onChange(e.target.value)} />
  ),
}));

const authValue: AuthContextValue = {
  loading: false,
  session: DEFAULT_SESSION,
  enabled: false,
  authenticated: false,
  username: null,
  login: vi.fn(),
  logout: vi.fn(),
  probeSession: vi.fn(),
  handleUnauthorized: vi.fn(),
};

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <MemoryRouter>
      <AuthContext.Provider value={authValue}>
        <QueryClientProvider client={client}>{ui}</QueryClientProvider>
      </AuthContext.Provider>
    </MemoryRouter>
  );
}

const minimalDevice: any = {
  id: 'abc',
  host_id: 'host1',
  pack_id: null,
  platform_id: null,
  name: 'Test Device',
  readiness_state: 'verified',
  missing_setup_fields: [],
};

describe('DeviceConfigEditor narrowed to appium_caps', () => {
  it('shows only appium_caps subkey in editor', async () => {
    vi.mocked(api.fetchDeviceConfig).mockResolvedValueOnce({
      appium_caps: { 'appium:cap': 1 },
      legacy_key: 'leftover',
    });
    vi.mocked(api.fetchConfigHistory).mockResolvedValueOnce([]);
    vi.mocked(driverPacksApi.fetchDriverPackCatalog).mockResolvedValueOnce([]);

    render(wrap(<DeviceConfigEditor device={minimalDevice} />));

    await waitFor(() => {
      const textarea = screen.getByTestId('monaco') as HTMLTextAreaElement;
      expect(textarea.value).toContain('appium:cap');
    });
    const textarea = screen.getByTestId('monaco') as HTMLTextAreaElement;
    expect(textarea.value).not.toContain('legacy_key');
  });

  it('hands off to SetupVerificationModal with replace_device_config: false after confirm', async () => {
    vi.mocked(api.fetchDeviceConfig).mockResolvedValueOnce({ appium_caps: { 'appium:cap': 1 } });
    vi.mocked(api.fetchConfigHistory).mockResolvedValueOnce([]);
    vi.mocked(driverPacksApi.fetchDriverPackCatalog).mockResolvedValueOnce([]);

    render(wrap(<DeviceConfigEditor device={minimalDevice} />));

    await waitFor(() => screen.getByTestId('monaco'));

    fireEvent.change(screen.getByTestId('monaco'), {
      target: { value: '{"appium:cap":2}' },
    });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    fireEvent.click(screen.getByRole('button', { name: /continue/i })); // confirm dialog

    await waitFor(() =>
      expect(screen.getByText('Save Config & Verify')).toBeDefined()
    );
  });
});
