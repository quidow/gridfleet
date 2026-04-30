import { useSearchParams } from 'react-router-dom';
import type { CursorDirection } from '../types';

const DEFAULT_PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

type QueryValue = string | number | null | undefined;

function readPositiveInt(searchParams: URLSearchParams, key: string, fallback: number): number {
  const raw = searchParams.get(key);
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function readAllowedInt(searchParams: URLSearchParams, key: string, allowed: readonly number[], fallback: number): number {
  const value = readPositiveInt(searchParams, key, fallback);
  return allowed.includes(value) ? value : fallback;
}

interface UseCursorQueryStateOptions {
  defaultPageSize: number;
  pageSizeOptions?: readonly number[];
}

export function useCursorQueryState({
  defaultPageSize,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
}: UseCursorQueryStateOptions) {
  const [searchParams, setSearchParams] = useSearchParams();

  const pageSize = readAllowedInt(searchParams, 'pageSize', pageSizeOptions, defaultPageSize);
  const cursor = searchParams.get('cursor') ?? '';
  const direction: CursorDirection = searchParams.get('cursorDirection') === 'newer' ? 'newer' : 'older';

  function updateParams(
    updates: Record<string, QueryValue>,
    options?: { resetCursor?: boolean; replace?: boolean },
  ) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);

      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === undefined || value === '') {
          next.delete(key);
        } else {
          next.set(key, String(value));
        }
      }

      if (options?.resetCursor) {
        next.delete('cursor');
        next.delete('cursorDirection');
      }

      return next;
    }, { replace: options?.replace ?? false });
  }

  function setPageSize(nextPageSize: number) {
    updateParams({ pageSize: nextPageSize }, { resetCursor: true });
  }

  function goOlder(nextCursor: string) {
    updateParams({ cursor: nextCursor, cursorDirection: 'older' });
  }

  function goNewer(nextCursor: string) {
    updateParams({ cursor: nextCursor, cursorDirection: 'newer' });
  }

  function resetToNewest() {
    updateParams({ cursor: null, cursorDirection: null });
  }

  return {
    searchParams,
    pageSize,
    cursor,
    direction,
    updateParams,
    setPageSize,
    goOlder,
    goNewer,
    resetToNewest,
  };
}
