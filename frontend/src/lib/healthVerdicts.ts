import type { HealthVerdictStatus } from '../types';

export const VERDICT_DOT_CLASSES: Record<HealthVerdictStatus, string> = {
  ok: 'bg-success-strong',
  warn: 'bg-warning-strong',
  failed: 'bg-danger-strong',
  unknown: 'bg-neutral-strong',
};

export const VERDICT_TEXT_CLASSES: Record<HealthVerdictStatus, string> = {
  ok: 'text-success-foreground',
  warn: 'text-warning-foreground',
  failed: 'text-danger-foreground',
  unknown: 'text-text-2',
};

export const VERDICT_STATUS_LABELS: Record<HealthVerdictStatus, string> = {
  ok: 'OK',
  warn: 'Warning',
  failed: 'Failed',
  unknown: 'Unknown',
};
