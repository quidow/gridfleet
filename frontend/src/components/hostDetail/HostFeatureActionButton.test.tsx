import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import HostFeatureActionButton from './HostFeatureActionButton';

const mockInvokeFeatureAction = vi.fn();

vi.mock('../../api/hostFeatureActions', () => ({
  invokeFeatureAction: (...args: unknown[]) => mockInvokeFeatureAction(...args),
}));

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const defaultProps = {
  hostId: 'host-1',
  packId: 'appium-uiautomator2',
  featureId: 'bugreport',
  action: { id: 'collect', label: 'Collect Bug Report' },
};

describe('HostFeatureActionButton', () => {
  beforeEach(() => {
    mockInvokeFeatureAction.mockReset();
  });

  it('renders action label', () => {
    render(<HostFeatureActionButton {...defaultProps} />);
    expect(screen.getByRole('button', { name: 'Collect Bug Report' })).toBeInTheDocument();
  });

  it('clicking calls invokeFeatureAction with correct ids', async () => {
    mockInvokeFeatureAction.mockResolvedValue({ ok: true, detail: '', data: {} });
    const user = userEvent.setup();

    render(<HostFeatureActionButton {...defaultProps} />);
    await user.click(screen.getByRole('button', { name: 'Collect Bug Report' }));

    await waitFor(() => {
      expect(mockInvokeFeatureAction).toHaveBeenCalledWith('host-1', 'appium-uiautomator2', 'bugreport', 'collect', {});
    });
  });

  it('disabled while in-flight', async () => {
    let resolve!: (value: { ok: boolean; detail: string; data: Record<string, unknown> }) => void;
    mockInvokeFeatureAction.mockReturnValue(new Promise((r) => { resolve = r; }));
    const user = userEvent.setup();

    render(<HostFeatureActionButton {...defaultProps} />);
    const btn = screen.getByRole('button', { name: 'Collect Bug Report' });
    await user.click(btn);

    // Button should be disabled while loading
    expect(btn).toBeDisabled();

    // Resolve the promise to clean up
    resolve({ ok: true, detail: '', data: {} });
    await waitFor(() => expect(btn).not.toBeDisabled());
  });

  it('error message shown on failure', async () => {
    mockInvokeFeatureAction.mockResolvedValue({ ok: false, detail: 'agent unreachable', data: {} });
    const user = userEvent.setup();

    render(<HostFeatureActionButton {...defaultProps} />);
    await user.click(screen.getByRole('button', { name: 'Collect Bug Report' }));

    await waitFor(() => {
      expect(screen.getByText('agent unreachable')).toBeInTheDocument();
    });
  });

  it('shows error on thrown exception', async () => {
    mockInvokeFeatureAction.mockRejectedValue(new Error('network error'));
    const user = userEvent.setup();

    render(<HostFeatureActionButton {...defaultProps} />);
    await user.click(screen.getByRole('button', { name: 'Collect Bug Report' }));

    await waitFor(() => {
      expect(screen.getByText('network error')).toBeInTheDocument();
    });
  });
});
