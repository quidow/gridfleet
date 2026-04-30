type RetriableQueryState = 'initial-loading' | 'retrying' | 'success' | 'error';

export interface RetriableQueryLike {
  status: 'pending' | 'error' | 'success';
  fetchStatus: 'idle' | 'fetching' | 'paused';
  failureCount: number;
}

const PRIORITY: Record<RetriableQueryState, number> = {
  error: 3,
  retrying: 2,
  'initial-loading': 1,
  success: 0,
};

export function deriveRetriableQueryState(q: RetriableQueryLike): RetriableQueryState {
  if (q.status === 'error') return 'error';
  if (q.status === 'pending') return 'initial-loading';
  if (q.fetchStatus === 'fetching' && q.failureCount > 0) return 'retrying';
  return 'success';
}

export function collapseRetriableQueryStates(states: RetriableQueryState[]): RetriableQueryState {
  if (states.length === 0) return 'success';
  return states.reduce<RetriableQueryState>(
    (worst, current) => (PRIORITY[current] > PRIORITY[worst] ? current : worst),
    'success',
  );
}
