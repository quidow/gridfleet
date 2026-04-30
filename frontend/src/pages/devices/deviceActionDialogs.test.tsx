import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DeviceActionErrorsDialog, TagsActionDialog } from './deviceActionDialogs';

describe('TagsActionDialog', () => {
  it('forwards textarea changes', async () => {
    const onTagsTextChange = vi.fn();
    render(
      <TagsActionDialog
        isOpen
        onClose={() => {}}
        title="Update Tags"
        tagsText="{}"
        merge
        mergeLabel="Merge"
        tagsError={null}
        onTagsTextChange={onTagsTextChange}
        onMergeChange={() => {}}
        onConfirm={() => {}}
      />,
    );

    await userEvent.type(screen.getByLabelText('Tags JSON'), ' ');

    expect(onTagsTextChange).toHaveBeenCalled();
  });

  it('shows error in danger tone', () => {
    render(
      <TagsActionDialog
        isOpen
        onClose={() => {}}
        title="Update Tags"
        tagsText="{"
        merge
        mergeLabel="Merge"
        tagsError="Invalid JSON"
        onTagsTextChange={() => {}}
        onMergeChange={() => {}}
        onConfirm={() => {}}
      />,
    );

    expect(screen.getByText('Invalid JSON').className).toMatch(/text-danger-foreground/);
  });
});

describe('DeviceActionErrorsDialog', () => {
  it('renders a footer Close action', async () => {
    const onClose = vi.fn();
    render(<DeviceActionErrorsDialog isOpen onClose={onClose} title="Action Errors" lines={[]} />);

    await userEvent.click(screen.getByRole('button', { name: 'Close' }));

    expect(onClose).toHaveBeenCalled();
  });
});
