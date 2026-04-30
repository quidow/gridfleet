import { describe, expect, it } from 'vitest';
import { EMPTY_GLYPH, formatStat } from './emptyValue';

describe('formatStat', () => {
  it('returns em-dash for null and undefined', () => {
    expect(formatStat(null)).toBe(EMPTY_GLYPH);
    expect(formatStat(undefined)).toBe(EMPTY_GLYPH);
  });

  it('returns stringified zero by default', () => {
    expect(formatStat(0)).toBe('0');
  });

  it('treats zero as empty when zeroIsEmpty is set', () => {
    expect(formatStat(0, { zeroIsEmpty: true })).toBe(EMPTY_GLYPH);
  });

  it('formats with Intl.NumberFormat when locale provided', () => {
    expect(formatStat(1234, { locale: 'en-US' })).toBe('1,234');
  });

  it('applies suffix if provided', () => {
    expect(formatStat(42, { suffix: '%' })).toBe('42%');
    expect(formatStat(null, { suffix: '%' })).toBe(EMPTY_GLYPH);
  });
});
