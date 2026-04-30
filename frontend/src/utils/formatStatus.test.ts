import { describe, expect, it } from 'vitest';
import { formatStatus } from './formatStatus';

describe('formatStatus', () => {
  it('title-cases simple status values', () => {
    expect(formatStatus('running')).toBe('Running');
    expect(formatStatus('passed')).toBe('Passed');
    expect(formatStatus('offline')).toBe('Offline');
  });

  it('replaces underscores with spaces and title-cases each word', () => {
    expect(formatStatus('recovery_backoff')).toBe('Recovery Backoff');
    expect(formatStatus('real_device')).toBe('Real Device');
  });

  it('trims values before formatting', () => {
    expect(formatStatus('  deferred_stop  ')).toBe('Deferred Stop');
  });

  it('handles empty, null, and undefined input', () => {
    expect(formatStatus('')).toBe('');
    expect(formatStatus(null)).toBe('');
    expect(formatStatus(undefined)).toBe('');
  });

  it('preserves known acronyms and product casing', () => {
    expect(formatStatus('ios')).toBe('iOS');
    expect(formatStatus('tvos')).toBe('tvOS');
    expect(formatStatus('firetv')).toBe('Fire TV');
    expect(formatStatus('adb')).toBe('ADB');
    expect(formatStatus('usb')).toBe('USB');
  });
});
