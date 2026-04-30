import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import CursorPagination from './CursorPagination';

describe('CursorPagination', () => {
  it('shows newest-page state and disables newer actions there', () => {
    render(
      <CursorPagination
        pageSize={50}
        nextCursor="older-cursor"
        prevCursor={null}
        isNewestPage
        onOlder={vi.fn()}
        onNewer={vi.fn()}
        onBackToNewest={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );

    expect(screen.getByText('Newest results first')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back to newest' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Older' })).toBeEnabled();
  });

  it('calls navigation handlers with the matching cursors', async () => {
    const onOlder = vi.fn();
    const onNewer = vi.fn();
    const onBackToNewest = vi.fn();
    render(
      <CursorPagination
        pageSize={25}
        nextCursor="older-cursor"
        prevCursor="newer-cursor"
        isNewestPage={false}
        onOlder={onOlder}
        onNewer={onNewer}
        onBackToNewest={onBackToNewest}
        onPageSizeChange={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Older' }));
    await userEvent.click(screen.getByRole('button', { name: 'Newer' }));
    await userEvent.click(screen.getByRole('button', { name: 'Back to newest' }));

    expect(onOlder).toHaveBeenCalledWith('older-cursor');
    expect(onNewer).toHaveBeenCalledWith('newer-cursor');
    expect(onBackToNewest).toHaveBeenCalled();
  });

  it('calls the page size handler when the page size changes', async () => {
    const onPageSizeChange = vi.fn();
    render(
      <CursorPagination
        pageSize={25}
        nextCursor={null}
        prevCursor={null}
        isNewestPage
        onOlder={vi.fn()}
        onNewer={vi.fn()}
        onBackToNewest={vi.fn()}
        onPageSizeChange={onPageSizeChange}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('Rows per page'), '100');

    expect(onPageSizeChange).toHaveBeenCalledWith(100);
  });
});
