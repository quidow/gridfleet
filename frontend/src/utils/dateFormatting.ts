const ISO_DATE_ONLY_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

const DATE_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat('en-GB', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const RELATIVE_TIME_FORMATTER = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });

function isValidDate(value: Date): boolean {
  return !Number.isNaN(value.getTime());
}

function parseIsoDateOnly(value: string): Date | null {
  const match = ISO_DATE_ONLY_PATTERN.exec(value);
  if (!match) return null;
  const [, year, month, day] = match;
  const candidate = new Date(Number(year), Number(month) - 1, Number(day));
  if (
    candidate.getFullYear() !== Number(year) ||
    candidate.getMonth() !== Number(month) - 1 ||
    candidate.getDate() !== Number(day)
  ) {
    return null;
  }
  return candidate;
}

function toDate(value: Date | string | null | undefined): Date | null {
  if (!value) return null;
  if (value instanceof Date) return isValidDate(value) ? value : null;
  if (ISO_DATE_ONLY_PATTERN.test(value)) {
    return parseIsoDateOnly(value);
  }
  const candidate = new Date(value);
  return isValidDate(candidate) ? candidate : null;
}

export function formatDateTime(value: Date | string | null | undefined): string {
  const date = toDate(value);
  return date ? DATE_TIME_FORMATTER.format(date) : '-';
}

export function formatDateOnly(value: Date | string | null | undefined): string {
  const date = toDate(value);
  return date ? DATE_FORMATTER.format(date) : '-';
}

export function formatRelativeTime(value: Date | string | null | undefined, nowMs = Date.now()): string {
  const date = toDate(value);
  if (!date) return '-';

  const diffSeconds = Math.round((date.getTime() - nowMs) / 1000);
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 60) return RELATIVE_TIME_FORMATTER.format(diffSeconds, 'second');
  const diffMinutes = Math.round(diffSeconds / 60);
  if (Math.abs(diffMinutes) < 60) return RELATIVE_TIME_FORMATTER.format(diffMinutes, 'minute');
  const diffHours = Math.round(diffMinutes / 60);
  if (Math.abs(diffHours) < 24) return RELATIVE_TIME_FORMATTER.format(diffHours, 'hour');
  const diffDays = Math.round(diffHours / 24);
  if (Math.abs(diffDays) < 7) return RELATIVE_TIME_FORMATTER.format(diffDays, 'day');
  const diffWeeks = Math.round(diffDays / 7);
  return RELATIVE_TIME_FORMATTER.format(diffWeeks, 'week');
}


export function isoToDateOnly(value: string | null | undefined): string {
  if (!value) return '';
  if (ISO_DATE_ONLY_PATTERN.test(value)) return value;
  const date = toDate(value);
  if (!date) return '';
  const year = String(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function dateOnlyToStartOfDayIso(value: string): string {
  return new Date(`${value}T00:00:00`).toISOString();
}

export function dateOnlyToEndOfDayIso(value: string): string {
  const date = new Date(`${value}T23:59:59.999`);
  return date.toISOString();
}

export function formatDuration(
  startISO: string,
  endISO: string | null,
  nowMs: number = Date.now(),
): string {
  const start = toDate(startISO);
  if (!start) return '-';
  const end = endISO ? toDate(endISO) : null;
  const endMs = end ? end.getTime() : nowMs;
  const diff = endMs - start.getTime();
  if (diff < 0) return '0s';
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}
