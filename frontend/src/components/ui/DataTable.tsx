import type { ReactNode } from 'react';
import { SortableHeader } from '../SortableHeader';
import { RowActionsMenu, type RowActionItem } from '../RowActionsMenu';
import EmptyState from './EmptyState';
import { Table } from 'lucide-react';

export type SortDirection = 'asc' | 'desc';

export interface DataTableSort<SortKey extends string = string> {
  key: SortKey;
  direction: SortDirection;
}

export interface DataTableColumn<Row, SortKey extends string = string> {
  key: string;
  header: ReactNode;
  sortKey?: SortKey;
  align?: 'left' | 'center' | 'right';
  width?: string;
  className?: string;
  headerClassName?: string;
  render: (row: Row, index: number) => ReactNode;
}

interface DataTableSelection<Row> {
  selectedKeys: Set<string | number>;
  onToggle: (row: Row) => void;
  onToggleAll?: (rows: Row[]) => void;
}

interface DataTableProps<Row, SortKey extends string = string> {
  columns: DataTableColumn<Row, SortKey>[];
  rows: Row[];
  rowKey: (row: Row) => string | number;
  loading?: boolean;
  error?: ReactNode;
  emptyState?: ReactNode;
  sort?: DataTableSort<SortKey>;
  onSortChange?: (next: DataTableSort<SortKey>) => void;
  onRowClick?: (row: Row) => void;
  rowClassName?: (row: Row) => string | undefined;
  selection?: DataTableSelection<Row>;
  rowActions?: (row: Row) => RowActionItem[];
  rowActionsLabel?: (row: Row) => string;
  density?: 'compact' | 'comfortable';
  stickyHeader?: boolean;
  caption?: string;
  rowTestId?: (row: Row) => string;
}

const ALIGN_CLASSES = { left: 'text-left', center: 'text-center', right: 'text-right' };
const CELL_PADDING = { compact: 'px-3 py-2', comfortable: 'px-5 py-3' };
const SKELETON_ROWS = 5;

// Generic DataTable component exported with a type-cast to preserve generics
function DataTableInner<Row, SortKey extends string = string>({
  columns,
  rows,
  rowKey,
  loading = false,
  error,
  emptyState,
  sort,
  onSortChange,
  onRowClick,
  rowClassName,
  selection,
  rowActions,
  rowActionsLabel,
  density = 'compact',
  stickyHeader = false,
  caption,
  rowTestId,
}: DataTableProps<Row, SortKey>) {
  const cellPad = CELL_PADDING[density];
  const hasRowActions = !!rowActions;
  const hasSelection = !!selection;

  function handleSortToggle(sortKey: SortKey) {
    if (!onSortChange) return;
    const nextDirection =
      sort?.key === sortKey && sort.direction === 'asc' ? 'desc' : 'asc';
    onSortChange({ key: sortKey, direction: nextDirection });
  }

  const headerRow = (
    <thead className={stickyHeader ? 'sticky top-0 z-10 bg-surface-2' : 'bg-surface-2'}>
      <tr>
        {hasSelection && (
          <th className={`w-10 ${cellPad}`}>
            <input
              type="checkbox"
              className="h-4 w-4 cursor-pointer rounded border-border-strong text-accent focus:ring-accent"
              checked={rows.length > 0 && selection.selectedKeys.size === rows.length}
              ref={(el) => {
                if (el) {
                  el.indeterminate =
                    selection.selectedKeys.size > 0 && selection.selectedKeys.size < rows.length;
                }
              }}
              onChange={() => selection.onToggleAll?.(rows)}
              aria-label="Select all rows"
            />
          </th>
        )}
        {columns.map((col) => (
          <th
            key={col.key}
            className={[
              cellPad,
              ALIGN_CLASSES[col.align ?? 'left'],
              col.headerClassName ?? '',
            ]
              .filter(Boolean)
              .join(' ')}
            style={col.width ? { width: col.width } : undefined}
          >
            {col.sortKey && onSortChange ? (
              <SortableHeader
                label={typeof col.header === 'string' ? col.header : ''}
                active={sort?.key === col.sortKey}
                direction={sort?.direction ?? 'asc'}
                onToggle={() => handleSortToggle(col.sortKey!)}
                align={col.align}
              />
            ) : (
              <span className="text-xs font-medium uppercase tracking-wide text-text-3">
                {col.header}
              </span>
            )}
          </th>
        ))}
        {hasRowActions && (
          <th className={`w-10 ${cellPad}`} aria-label="Actions" />
        )}
      </tr>
    </thead>
  );

  // Loading state: skeleton rows
  if (loading) {
    return (
      <div className="overflow-x-auto rounded-lg border border-border bg-surface-1">
        <table className="min-w-full divide-y divide-border" aria-label={caption} aria-busy="true">
          {headerRow}
          <tbody className="divide-y divide-border">
            {Array.from({ length: SKELETON_ROWS }).map((_, i) => (
              <tr key={i}>
                {hasSelection && <td className={cellPad}><div className="h-4 w-4 rounded bg-border animate-pulse" /></td>}
                {columns.map((col) => (
                  <td key={col.key} className={cellPad}>
                    <div className="h-4 rounded bg-border animate-pulse" style={{ width: '70%' }} />
                  </td>
                ))}
                {hasRowActions && <td className={cellPad} />}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // Error state
  if (error) {
    return (
      <div className="overflow-x-auto rounded-lg border border-border bg-surface-1">
        <table className="min-w-full divide-y divide-border" aria-label={caption}>
          {headerRow}
          <tbody>
            <tr>
              <td
                colSpan={columns.length + (hasSelection ? 1 : 0) + (hasRowActions ? 1 : 0)}
                className="px-5 py-8 text-center"
              >
                {error}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    );
  }

  // Empty state
  if (rows.length === 0) {
    return (
      <div className="overflow-x-auto rounded-lg border border-border bg-surface-1">
        <table className="min-w-full divide-y divide-border" aria-label={caption}>
          {headerRow}
          <tbody>
            <tr>
              <td
                colSpan={columns.length + (hasSelection ? 1 : 0) + (hasRowActions ? 1 : 0)}
                className="px-5 py-0"
              >
                {emptyState ?? (
                  <EmptyState icon={Table} title="No data" />
                )}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-surface-1">
      <table
        className="min-w-full divide-y divide-border"
        aria-label={caption}
      >
        {headerRow}
        <tbody className="divide-y divide-border">
          {rows.map((row, index) => {
            const key = rowKey(row);
            const isSelected = selection?.selectedKeys.has(key);
            return (
              <tr
                key={key}
                data-testid={rowTestId?.(row)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={[
                  'hover:bg-surface-2',
                  onRowClick ? 'cursor-pointer' : '',
                  isSelected ? 'bg-accent-soft' : '',
                  rowClassName?.(row) ?? '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                {hasSelection && (
                  <td
                    className={cellPad}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <input
                      type="checkbox"
                      className="h-4 w-4 cursor-pointer rounded border-border-strong text-accent focus:ring-accent"
                      checked={!!isSelected}
                      onChange={() => selection.onToggle(row)}
                      aria-label={`Select row ${index + 1}`}
                    />
                  </td>
                )}
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={[
                      cellPad,
                      'text-sm text-text-1',
                      ALIGN_CLASSES[col.align ?? 'left'],
                      col.className ?? '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                  >
                    {col.render(row, index)}
                  </td>
                ))}
                {hasRowActions && (
                  <td
                    className={`${cellPad} text-right`}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <RowActionsMenu label={rowActionsLabel?.(row) ?? 'Row actions'} items={rowActions(row)} />
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// Cast to preserve generics at call sites
const DataTable = DataTableInner as <Row, SortKey extends string = string>(
  props: DataTableProps<Row, SortKey>,
) => React.JSX.Element;

export default DataTable;

// Required for the cast above
import * as React from 'react';
