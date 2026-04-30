import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RunActionButtons from './RunActionButtons';

describe('RunActionButtons', () => {
  it('renders Cancel and Force Release as buttons', () => {
    render(<RunActionButtons onCancel={() => {}} onForceRelease={() => {}} />);
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Force Release' })).toBeInTheDocument();
  });

  it('invokes onCancel when Cancel clicked', async () => {
    const onCancel = vi.fn();
    const onForceRelease = vi.fn();
    render(<RunActionButtons onCancel={onCancel} onForceRelease={onForceRelease} />);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onForceRelease).not.toHaveBeenCalled();
  });

  it('invokes onForceRelease when Force Release clicked', async () => {
    const onCancel = vi.fn();
    const onForceRelease = vi.fn();
    render(<RunActionButtons onCancel={onCancel} onForceRelease={onForceRelease} />);
    await userEvent.click(screen.getByRole('button', { name: 'Force Release' }));
    expect(onForceRelease).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });
});
