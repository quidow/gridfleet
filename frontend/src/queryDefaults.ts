import type { Query } from '@tanstack/react-query';

export function shouldThrowOnError(_error: Error, query: Query): boolean {
  return !query.meta?.handleErrorLocally;
}
