import Button from './Button';

interface PaginationProps {
  page: number;
  pageSize: number;
  total?: number | null;
  pageSizeOptions?: readonly number[];
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  className?: string;
}

const DEFAULT_PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

export default function Pagination({
  page,
  pageSize,
  total,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
  onPageChange,
  onPageSizeChange,
  className = '',
}: PaginationProps) {
  const hasTotal = typeof total === 'number';
  const safeTotal = hasTotal ? total : 0;
  const totalPages = hasTotal ? Math.max(1, Math.ceil(safeTotal / pageSize)) : null;
  const clampedPage = totalPages ? Math.min(page, totalPages) : page;
  const start = hasTotal
    ? (safeTotal === 0 ? 0 : (clampedPage - 1) * pageSize + 1)
    : (clampedPage - 1) * pageSize + 1;
  const end = hasTotal
    ? (safeTotal === 0 ? 0 : Math.min(safeTotal, clampedPage * pageSize))
    : clampedPage * pageSize;
  const canGoBack = clampedPage > 1;
  const canGoForward = totalPages ? clampedPage < totalPages : true;

  return (
    <div
      className={[
        'mt-4 flex flex-col gap-3 rounded-lg border border-border bg-surface-1 px-4 py-3 md:flex-row md:items-center md:justify-between',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="text-sm text-text-2">
        Showing {start}-{end}
        {hasTotal ? ` of ${safeTotal}` : ''}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <label className="flex items-center gap-2 text-sm text-text-2">
          <span>Rows per page</span>
          <select
            aria-label="Rows per page"
            value={pageSize}
            onChange={(event) => onPageSizeChange(Number(event.target.value))}
            className="rounded-md border border-border-strong px-2 py-1.5 text-sm text-text-2 focus:outline-none focus:ring-2 focus:ring-accent"
          >
            {pageSizeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => onPageChange(1)} disabled={!canGoBack}>
            First
          </Button>
          <Button variant="secondary" size="sm" onClick={() => onPageChange(clampedPage - 1)} disabled={!canGoBack}>
            Prev
          </Button>
          <span className="min-w-20 text-center text-sm text-text-2">
            Page {clampedPage}
            {totalPages ? ` of ${totalPages}` : ''}
          </span>
          <Button variant="secondary" size="sm" onClick={() => onPageChange(clampedPage + 1)} disabled={!canGoForward}>
            Next
          </Button>
          {totalPages ? (
            <Button variant="secondary" size="sm" onClick={() => onPageChange(totalPages)} disabled={!canGoForward}>
              Last
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
