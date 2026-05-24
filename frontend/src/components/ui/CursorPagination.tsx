import { Button } from './Button';
import { Card } from './Card';
import { Select } from './Select';

interface CursorPaginationProps {
  pageSize: number;
  pageSizeOptions?: readonly number[];
  nextCursor: string | null;
  prevCursor: string | null;
  isNewestPage: boolean;
  onOlder: (cursor: string) => void;
  onNewer: (cursor: string) => void;
  onBackToNewest: () => void;
  onPageSizeChange: (pageSize: number) => void;
  className?: string;
}

const DEFAULT_PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

export function CursorPagination({
  pageSize,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
  nextCursor,
  prevCursor,
  isNewestPage,
  onOlder,
  onNewer,
  onBackToNewest,
  onPageSizeChange,
  className = '',
}: CursorPaginationProps) {
  return (
    <Card
      padding="none"
      className={[
        'mt-4 flex flex-col gap-3 px-4 py-3 md:flex-row md:items-center md:justify-between',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="text-sm text-text-2">
        {isNewestPage ? 'Newest results first' : 'Viewing historical results'}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <label className="flex items-center gap-2 text-sm text-text-2">
          <span>Rows per page</span>
          <Select
            ariaLabel="Rows per page"
            value={String(pageSize)}
            onChange={(value) => onPageSizeChange(Number(value))}
            size="sm"
            options={pageSizeOptions.map((option) => ({
              value: String(option),
              label: String(option),
            }))}
          />
        </label>

        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={onBackToNewest} disabled={isNewestPage}>
            Back to newest
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => prevCursor && onNewer(prevCursor)}
            disabled={!prevCursor}
          >
            Newer
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => nextCursor && onOlder(nextCursor)}
            disabled={!nextCursor}
          >
            Older
          </Button>
        </div>
      </div>
    </Card>
  );
}
