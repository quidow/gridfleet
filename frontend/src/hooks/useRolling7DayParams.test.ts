import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useRolling7DayParams } from './useRolling7DayParams';

describe('useRolling7DayParams', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date('2026-06-11T12:30:00Z'));
  });
  afterEach(() => vi.useRealTimers());

  it('returns an hour-aligned rolling 7-day window', () => {
    const { result } = renderHook(() => useRolling7DayParams());
    expect(result.current.date_to).toBe('2026-06-11T13:00:00.000Z');
    expect(result.current.date_from).toBe('2026-06-04T13:00:00.000Z');
  });
});
