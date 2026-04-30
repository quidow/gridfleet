import { describe, it, expect } from 'vitest';
import {
  collapseRetriableQueryStates,
  deriveRetriableQueryState,
  type RetriableQueryLike,
} from './useRetriableQueryState';

function q(partial: Partial<RetriableQueryLike>): RetriableQueryLike {
  return {
    status: 'pending',
    fetchStatus: 'fetching',
    failureCount: 0,
    ...partial,
  };
}

describe('deriveRetriableQueryState', () => {
  it('treats initial pending fetch as initial-loading', () => {
    expect(deriveRetriableQueryState(q({ status: 'pending', fetchStatus: 'fetching', failureCount: 0 })))
      .toBe('initial-loading');
  });

  it('keeps pending through mid-retry as initial-loading', () => {
    expect(deriveRetriableQueryState(q({ status: 'pending', fetchStatus: 'fetching', failureCount: 2 })))
      .toBe('initial-loading');
  });

  it('returns success when fetch settles idle after success', () => {
    expect(deriveRetriableQueryState(q({ status: 'success', fetchStatus: 'idle', failureCount: 0 })))
      .toBe('success');
  });

  it('returns retrying when background refetch fails after prior success', () => {
    expect(deriveRetriableQueryState(q({ status: 'success', fetchStatus: 'fetching', failureCount: 2 })))
      .toBe('retrying');
  });

  it('background refetch in flight with no failures stays success', () => {
    expect(deriveRetriableQueryState(q({ status: 'success', fetchStatus: 'fetching', failureCount: 0 })))
      .toBe('success');
  });

  it('returns error when retries exhausted', () => {
    expect(deriveRetriableQueryState(q({ status: 'error', fetchStatus: 'idle', failureCount: 3 })))
      .toBe('error');
  });
});

describe('collapseRetriableQueryStates', () => {
  it('returns success for empty list', () => {
    expect(collapseRetriableQueryStates([])).toBe('success');
  });

  it('returns success when all successful', () => {
    expect(collapseRetriableQueryStates(['success', 'success'])).toBe('success');
  });

  it('picks initial-loading over success', () => {
    expect(collapseRetriableQueryStates(['success', 'initial-loading', 'success']))
      .toBe('initial-loading');
  });

  it('picks retrying over initial-loading', () => {
    expect(collapseRetriableQueryStates(['initial-loading', 'retrying'])).toBe('retrying');
  });

  it('picks error over everything else', () => {
    expect(collapseRetriableQueryStates(['success', 'retrying', 'initial-loading', 'error']))
      .toBe('error');
  });
});
