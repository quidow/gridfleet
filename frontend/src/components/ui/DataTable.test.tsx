import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import DataTable from './DataTable';
import type { DataTableColumn } from './DataTable';

interface Row {
  id: string;
  name: string;
  status: string;
}

const rows: Row[] = [
  { id: '1', name: 'Alice', status: 'active' },
  { id: '2', name: 'Bob', status: 'inactive' },
];

const columns: DataTableColumn<Row, 'name' | 'status'>[] = [
  {
    key: 'name',
    header: 'Name',
    sortKey: 'name',
    render: (row) => row.name,
  },
  {
    key: 'status',
    header: 'Status',
    sortKey: 'status',
    render: (row) => row.status,
  },
];

describe('DataTable', () => {
  it('renders column headers', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ key: 'name', direction: 'asc' }}
        onSortChange={vi.fn()}
      />,
    );
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('Status')).toBeInTheDocument();
  });

  it('renders row data', () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();
  });

  it('defaults to compact density', () => {
    const simpleColumns = [{ key: 'a', header: 'A', render: (row: { a: string }) => row.a }];
    const { container } = render(
      <DataTable columns={simpleColumns} rows={[{ a: 'x' }]} rowKey={(row) => row.a} />,
    );
    const cell = container.querySelector('tbody td');
    expect(cell?.className).toMatch(/px-3 py-2/);
    expect(cell?.className).not.toMatch(/px-5 py-3/);
  });

  it('shows emptyState when rows is empty', () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        rowKey={(r) => r.id}
        emptyState={<p>Nothing here</p>}
      />,
    );
    expect(screen.getByText('Nothing here')).toBeInTheDocument();
  });

  it('shows skeleton rows when loading=true', () => {
    render(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} loading />);
    const table = screen.getByRole('table');
    // Should have header + 5 skeleton rows
    const bodyRows = within(table).getAllByRole('row').slice(1); // skip header
    expect(bodyRows).toHaveLength(5);
  });

  it('calls onSortChange with toggled direction when sort header is clicked', async () => {
    const onSortChange = vi.fn();
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ key: 'name', direction: 'asc' }}
        onSortChange={onSortChange}
      />,
    );
    await userEvent.click(screen.getByText('Name'));
    expect(onSortChange).toHaveBeenCalledWith({ key: 'name', direction: 'desc' });
  });

  it('calls onSortChange with asc when a different column is clicked', async () => {
    const onSortChange = vi.fn();
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        sort={{ key: 'name', direction: 'asc' }}
        onSortChange={onSortChange}
      />,
    );
    await userEvent.click(screen.getByText('Status'));
    expect(onSortChange).toHaveBeenCalledWith({ key: 'status', direction: 'asc' });
  });

  it('calls selection.onToggle when row checkbox is clicked', async () => {
    const onToggle = vi.fn();
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        selection={{
          selectedKeys: new Set(),
          onToggle,
          onToggleAll: vi.fn(),
        }}
      />,
    );
    const checkboxes = screen.getAllByRole('checkbox');
    // First is "select all", rest are row checkboxes
    await userEvent.click(checkboxes[1]);
    expect(onToggle).toHaveBeenCalledWith(rows[0]);
  });

  it('row-action click does not fire onRowClick', async () => {
    const onRowClick = vi.fn();

    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        onRowClick={onRowClick}
        rowActions={() => [
          {
            key: 'delete',
            label: 'Delete',
            icon: null,
            onSelect: vi.fn(),
          },
        ]}
      />,
    );
    // Click on the row-actions trigger button (MoreVertical)
    const actionButtons = screen.getAllByLabelText('Row actions');
    await userEvent.click(actionButtons[0]);
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it('applies rowTestId to each rendered <tr>', () => {
    const rows = [
      { id: 'a', name: 'Alpha' },
      { id: 'b', name: 'Beta' },
    ];
    const columns = [
      { key: 'name', header: 'Name', render: (row: { name: string }) => row.name },
    ];
    const { container } = render(
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        rowTestId={(row) => `row-${row.id}`}
      />,
    );
    expect(container.querySelector('[data-testid="row-a"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="row-b"]')).not.toBeNull();
  });
});
