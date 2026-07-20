import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DeviceActionErrorsDialog } from './deviceActionDialogs';

describe('DeviceActionErrorsDialog', () => {
  it('renders a footer Close action', async () => {
    const onClose = vi.fn();
    render(<DeviceActionErrorsDialog isOpen onClose={onClose} title="Action Errors" lines={[]} />);

    await userEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(onClose).toHaveBeenCalled();
  });
});
