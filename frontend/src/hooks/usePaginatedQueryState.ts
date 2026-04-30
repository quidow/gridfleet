import { useSearchParams } from 'react-router-dom';
import type { SortDirection } from '../types';

const DEFAULT_PAGE = 1;
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

function readAllowedString<T extends string>(
  searchParams: URLSearchParams,
  key: string,
  allowed: readonly T[],
  fallback: T,
): T {
  const raw = searchParams.get(key);
  return raw && allowed.includes(raw as T) ? (raw as T) : fallback;
}

interface UsePaginatedQueryStateOptions<SortKey extends string> {
  defaultPageSize: number;
  pageSizeOptions?: readonly number[];
  allowedSortKeys?: readonly SortKey[];
  defaultSortKey?: SortKey;
  defaultSortDirection?: SortDirection;
}

export function usePaginatedQueryState<SortKey extends string = never>({
  defaultPageSize,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
  allowedSortKeys,
  defaultSortKey,
  defaultSortDirection = 'desc',
}: UsePaginatedQueryStateOptions<SortKey>) {
  const [searchParams, setSearchParams] = useSearchParams();

  const page = readPositiveInt(searchParams, 'page', DEFAULT_PAGE);
  const pageSize = readAllowedInt(searchParams, 'pageSize', pageSizeOptions, defaultPageSize);
  const sort = allowedSortKeys && defaultSortKey
    ? readAllowedString(searchParams, 'sort', allowedSortKeys, defaultSortKey)
    : undefined;
  const direction = allowedSortKeys && defaultSortKey
    ? readAllowedString(searchParams, 'direction', ['asc', 'desc'] as const, defaultSortDirection)
    : undefined;

  function updateParams(updates: Record<string, QueryValue>, options?: { resetPage?: boolean; replace?: boolean }) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);

      for (const [key, value] of Object.entries(updates)) {
        if (value === null || value === undefined || value === '') {
          next.delete(key);
        } else {
          next.set(key, String(value));
        }
      }

      if (options?.resetPage && !Object.hasOwn(updates, 'page')) {
        next.set('page', '1');
      }

      return next;
    }, { replace: options?.replace ?? false });
  }

  function setPage(nextPage: number) {
    updateParams({ page: Math.max(DEFAULT_PAGE, nextPage) });
  }

  function setPageSize(nextPageSize: number) {
    updateParams({ pageSize: nextPageSize }, { resetPage: true });
  }

  function setSort(nextSort: SortKey, nextDirection: SortDirection) {
    if (!allowedSortKeys || !defaultSortKey) return;
    updateParams(
      {
        sort: nextSort,
        direction: nextDirection,
      },
      { resetPage: true },
    );
  }

  return {
    searchParams,
    page,
    pageSize,
    sort,
    direction,
    updateParams,
    setPage,
    setPageSize,
    setSort,
  };
}
