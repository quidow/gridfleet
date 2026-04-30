export const EMPTY_GLYPH = '—';

interface FormatStatOptions {
  zeroIsEmpty?: boolean;
  locale?: string;
  suffix?: string;
}

export function formatStat(
  value: number | null | undefined,
  opts: FormatStatOptions = {},
): string {
  if (value === null || value === undefined) return EMPTY_GLYPH;
  if (opts.zeroIsEmpty && value === 0) return EMPTY_GLYPH;

  const formatted = opts.locale
    ? new Intl.NumberFormat(opts.locale).format(value)
    : String(value);

  return opts.suffix ? `${formatted}${opts.suffix}` : formatted;
}
