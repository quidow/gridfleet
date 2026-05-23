import type { Query } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { shouldThrowOnError } from './queryDefaults';

function fakeQuery(meta?: Record<string, unknown>): Query {
  return { meta } as unknown as Query;
}

describe('shouldThrowOnError', () => {
  it('throws for queries with no meta', () => {
    expect(shouldThrowOnError(new Error(), fakeQuery())).toBe(true);
  });

  it('throws for queries with unrelated meta', () => {
    expect(shouldThrowOnError(new Error(), fakeQuery({ other: true }))).toBe(true);
  });

  it('does not throw when handleErrorLocally is true', () => {
    expect(shouldThrowOnError(new Error(), fakeQuery({ handleErrorLocally: true }))).toBe(false);
  });

  it('throws when handleErrorLocally is explicitly false', () => {
    expect(shouldThrowOnError(new Error(), fakeQuery({ handleErrorLocally: false }))).toBe(true);
  });
});
