import { describe, expect, it } from 'vitest';
import { formatEventDetails, SEEDED_EVENT_TYPES } from './eventRegistry';

describe('eventRegistry', () => {
  it('covers every full-demo seeded event type', () => {
    expect(SEEDED_EVENT_TYPES).toEqual([
      'run.completed',
      'run.failed',
      'run.cancelled',
      'host.offline',
      'host.online',
      'device.maintenance_start',
      'device.maintenance_end',
      'webhook.delivered',
      'webhook.failed',
      'config.updated',
      'session.stuck',
      'device.verified',
      'lifecycle.incident_open',
      'lifecycle.incident_resolved',
      'node.crash',
      'node.restart',
    ]);

    for (const type of SEEDED_EVENT_TYPES) {
      const formatted = formatEventDetails(type, { seed: true, event_type: type });
      expect(formatted.kind).toBe('text');
      expect(formatted.text).not.toContain('undefined');
    }
  });

  it('formats named run and host events as sentences', () => {
    expect(formatEventDetails('run.completed', { name: 'live-run-00' })).toEqual({
      kind: 'text',
      text: 'live-run-00 completed',
    });
    expect(formatEventDetails('run.failed', { name: 'live-run-01', reason: 'heartbeat timeout' })).toEqual({
      kind: 'text',
      text: 'live-run-01 failed: heartbeat timeout',
    });
    expect(formatEventDetails('host.offline', { hostname: 'lab-linux-02' })).toEqual({
      kind: 'text',
      text: 'lab-linux-02 went offline',
    });
  });

  it('formats node and lifecycle events without leaking undefined', () => {
    expect(formatEventDetails('node.crash', { device_name: 'Pixel 7' })).toEqual({
      kind: 'text',
      text: 'Appium node for Pixel 7 crashed',
    });
    expect(formatEventDetails('lifecycle.incident_open', { device_name: 'Pixel 7' })).toEqual({
      kind: 'text',
      text: 'Incident opened: Pixel 7',
    });
  });

  it('falls back to raw JSON for unknown non-empty payloads', () => {
    const formatted = formatEventDetails('new.event', { answer: 42 });
    expect(formatted.kind).toBe('json');
    expect(formatted.text).toContain('"answer": 42');
    expect(formatted.text).not.toContain(': undefined');
  });

  it('returns no-details fallback for unknown empty payloads', () => {
    expect(formatEventDetails('new.event', {})).toEqual({ kind: 'empty', text: 'No details' });
  });

});

import { resolveEventSeverity, legacyFallbackSeverity } from './eventRegistry';

describe('resolveEventSeverity', () => {
  it('returns the event severity when present', () => {
    const event = {
      id: 'e1',
      type: 'device.operational_state_changed',
      timestamp: 'now',
      severity: 'success' as const,
      data: {},
    };
    expect(resolveEventSeverity(event)).toBe('success');
  });

  it('falls back to legacy map when severity is null', () => {
    const event = {
      id: 'e1',
      type: 'node.crash',
      timestamp: 'now',
      severity: null,
      data: {},
    };
    expect(resolveEventSeverity(event)).toBe('critical');
  });

  it('falls back to neutral when both are unknown', () => {
    const event = {
      id: 'e1',
      type: 'not.a.real.event',
      timestamp: 'now',
      severity: null,
      data: {},
    };
    expect(resolveEventSeverity(event)).toBe('neutral');
  });

  it('legacyFallbackSeverity returns null for unknown types', () => {
    expect(legacyFallbackSeverity('not.a.real.event')).toBeNull();
  });
});
