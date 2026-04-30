import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePersistedBoolean } from './usePersistedBoolean';

describe('usePersistedBoolean', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns defaultValue when key is not in storage', () => {
    const { result } = renderHook(() => usePersistedBoolean('test-key', false));
    expect(result.current[0]).toBe(false);
  });

  it('returns true defaultValue when specified', () => {
    const { result } = renderHook(() => usePersistedBoolean('test-key', true));
    expect(result.current[0]).toBe(true);
  });

  it('reads initial value from localStorage', () => {
    localStorage.setItem('test-key', JSON.stringify(true));
    const { result } = renderHook(() => usePersistedBoolean('test-key', false));
    expect(result.current[0]).toBe(true);
  });

  it('updates state and writes to localStorage on set', () => {
    const { result } = renderHook(() => usePersistedBoolean('test-key', false));
    act(() => { result.current[1](true); });
    expect(result.current[0]).toBe(true);
    expect(JSON.parse(localStorage.getItem('test-key')!)).toBe(true);
  });

  it('falls back to defaultValue when localStorage contains invalid JSON', () => {
    localStorage.setItem('test-key', 'not-json{{');
    const { result } = renderHook(() => usePersistedBoolean('test-key', true));
    expect(result.current[0]).toBe(true);
  });

  it('still updates in-memory state when localStorage.setItem throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('QuotaExceededError');
    });
    const { result } = renderHook(() => usePersistedBoolean('test-key', false));
    act(() => { result.current[1](true); });
    expect(result.current[0]).toBe(true);
    spy.mockRestore();
  });
});
