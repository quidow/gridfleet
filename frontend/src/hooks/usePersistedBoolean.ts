import { useState } from 'react';

function readFromStorage(key: string, defaultValue: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return defaultValue;
    return JSON.parse(raw) === true;
  } catch {
    return defaultValue;
  }
}

/**
 * A boolean state hook that persists its value to localStorage.
 * Falls back to `defaultValue` when localStorage is unavailable or corrupt.
 */
export function usePersistedBoolean(
  key: string,
  defaultValue: boolean,
): [boolean, (next: boolean) => void] {
  const [value, setValue] = useState<boolean>(() => readFromStorage(key, defaultValue));

  function set(next: boolean) {
    setValue(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // localStorage unavailable or QuotaExceededError — UI state still updates in-memory.
    }
  }

  return [value, set];
}
