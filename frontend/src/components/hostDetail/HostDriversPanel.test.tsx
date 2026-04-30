import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import HostDriversPanel from './HostDriversPanel';

const mockUseHostDriverPacks = vi.fn();
const mockUseDriverPackCatalog = vi.fn();
const mockInvokeFeatureAction = vi.fn();

vi.mock('../../hooks/useDriverPacks', () => ({
  useHostDriverPacks: (...args: unknown[]) => mockUseHostDriverPacks(...args),
  useDriverPackCatalog: (...args: unknown[]) => mockUseDriverPackCatalog(...args),
}));

vi.mock('../../api/hostFeatureActions', () => ({
  invokeFeatureAction: (...args: unknown[]) => mockInvokeFeatureAction(...args),
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

describe('HostDriversPanel', () => {
  it('uses Drivers wording for empty host status', () => {
    mockUseHostDriverPacks.mockReturnValue({ data: { packs: [], runtimes: [], doctor: [] }, isLoading: false });
    mockUseDriverPackCatalog.mockReturnValue({ data: [] });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    expect(screen.getByText('No drivers installed. Enable drivers in Settings.')).toBeInTheDocument();
  });

  it('renders feature action buttons for installed packs with features', () => {
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'appium-uiautomator2',
            pack_release: '2026.04.0',
            runtime_id: null,
            status: 'installed',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: null,
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-uiautomator2',
          display_name: 'Appium UiAutomator2',
          state: 'enabled',
          current_release: '2026.04.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
          features: {
            bugreport: {
              display_name: 'Bug Report',
              description_md: '',
              actions: [
                { id: 'collect', label: 'Collect Bug Report' },
              ],
            },
          },
        },
      ],
    });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    expect(screen.getByRole('button', { name: 'Collect Bug Report' })).toBeInTheDocument();
  });

  it('renders feature health from host driver pack status', () => {
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'uploaded-sidecar-pack',
            pack_release: '1.0.0',
            runtime_id: null,
            status: 'installed',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: null,
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
        features: [
          {
            pack_id: 'uploaded-sidecar-pack',
            feature_id: 'tunnel',
            ok: false,
            detail: 'tunnel down',
          },
        ],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'uploaded-sidecar-pack',
          display_name: 'Uploaded Sidecar Pack',
          state: 'enabled',
          current_release: '1.0.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
          features: {
            tunnel: {
              display_name: 'Tunnel',
              description_md: '',
              actions: [{ id: 'restart', label: 'Restart Tunnel' }],
            },
          },
        },
      ],
    });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    expect(screen.getByText('Tunnel')).toBeInTheDocument();
    expect(screen.getByText('tunnel down')).toBeInTheDocument();
  });

  it('does not render feature action buttons for non-installed packs', () => {
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'appium-uiautomator2',
            pack_release: '2026.04.0',
            runtime_id: null,
            status: 'blocked',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: 'some reason',
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-uiautomator2',
          display_name: 'Appium UiAutomator2',
          state: 'enabled',
          current_release: '2026.04.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
          features: {
            bugreport: {
              display_name: 'Bug Report',
              description_md: '',
              actions: [{ id: 'collect', label: 'Collect Bug Report' }],
            },
          },
        },
      ],
    });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    expect(screen.queryByRole('button', { name: 'Collect Bug Report' })).not.toBeInTheDocument();
  });

  it('clicking a feature action button invokes the action', async () => {
    mockInvokeFeatureAction.mockResolvedValue({ ok: true, detail: '', data: {} });
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'appium-uiautomator2',
            pack_release: '2026.04.0',
            runtime_id: null,
            status: 'installed',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: null,
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-uiautomator2',
          display_name: 'Appium UiAutomator2',
          state: 'enabled',
          current_release: '2026.04.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
          features: {
            bugreport: {
              display_name: 'Bug Report',
              description_md: '',
              actions: [{ id: 'collect', label: 'Collect Bug Report' }],
            },
          },
        },
      ],
    });

    const user = userEvent.setup();
    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    const btn = screen.getByRole('button', { name: 'Collect Bug Report' });
    await user.click(btn);

    expect(mockInvokeFeatureAction).toHaveBeenCalledWith(
      'host-1',
      'appium-uiautomator2',
      'bugreport',
      'collect',
      {},
    );
  });

  it('renders no feature buttons for packs without features', () => {
    mockUseHostDriverPacks.mockReturnValue({
      data: {
        packs: [
          {
            pack_id: 'appium-xcuitest',
            pack_release: '2026.04.0',
            runtime_id: null,
            status: 'installed',
            resolved_install_spec: null,
            installer_log_excerpt: null,
            resolver_version: null,
            blocked_reason: null,
            installed_at: null,
            desired_appium_driver_version: null,
            installed_appium_driver_version: null,
            appium_driver_drift: false,
          },
        ],
        runtimes: [],
        doctor: [],
      },
      isLoading: false,
    });
    mockUseDriverPackCatalog.mockReturnValue({
      data: [
        {
          id: 'appium-xcuitest',
          display_name: 'Appium XCUITest',
          state: 'enabled',
          current_release: '2026.04.0',
          active_runs: 0,
          live_sessions: 0,
          runtime_policy: { strategy: 'recommended' },
          features: {},
        },
      ],
    });

    render(<HostDriversPanel hostId="host-1" hostOnline={true} />);

    // Only the pack_id text exists, no action buttons
    expect(screen.getByText('appium-xcuitest')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
