import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import Pagination from './Pagination';

describe('Pagination', () => {
  it('renders the current range and total', () => {
    render(
      <Pagination
        page={2}
        pageSize={25}
        total={60}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );

    expect(screen.getByText('Showing 26-50 of 60')).toBeInTheDocument();
    expect(screen.getByText('Page 2 of 3')).toBeInTheDocument();
  });

  it('disables first and prev on the first page', () => {
    render(
      <Pagination
        page={1}
        pageSize={25}
        total={10}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: 'First' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();
  });

  it('calls page handlers for navigation buttons', async () => {
    const onPageChange = vi.fn();
    render(
      <Pagination
        page={2}
        pageSize={25}
        total={80}
        onPageChange={onPageChange}
        onPageSizeChange={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await userEvent.click(screen.getByRole('button', { name: 'Last' }));

    expect(onPageChange).toHaveBeenNthCalledWith(1, 3);
    expect(onPageChange).toHaveBeenNthCalledWith(2, 4);
  });

  it('calls page size handler when the page size changes', async () => {
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        page={1}
        pageSize={25}
        total={80}
        onPageChange={vi.fn()}
        onPageSizeChange={onPageSizeChange}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText('Rows per page'), '100');

    expect(onPageSizeChange).toHaveBeenCalledWith(100);
  });

  it('hides the last button when total is unknown', () => {
    render(
      <Pagination
        page={2}
        pageSize={25}
        total={null}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: 'Last' })).not.toBeInTheDocument();
    expect(screen.getByText('Showing 26-50')).toBeInTheDocument();
  });
});
