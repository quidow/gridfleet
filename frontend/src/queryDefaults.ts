import type { Query } from '@tanstack/react-query';

/**
 * Throw fetch errors into the nearest ErrorBoundary by default.
 * Hooks that want to display errors inline opt out with
 * `meta: { handleErrorLocally: true }`.
 */
export function shouldThrowOnError(_error: Error, query: Query): boolean {
  return !query.meta?.handleErrorLocally;
}
