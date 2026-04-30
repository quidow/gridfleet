import { describe, expect, it } from 'vitest';
import { composeDeviceStatusNarrative } from './deviceStatusNarrative';
import type { DeviceRead } from '../../types';

const baseDevice = {
  availability_status: 'available',
  lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
  readiness_state: 'verified',
  missing_setup_fields: [],
  health_summary: { healthy: true, summary: 'Healthy', last_checked_at: null },
  needs_attention: false,
} as unknown as DeviceRead;

describe('composeDeviceStatusNarrative', () => {
  it('available + healthy', () => {
    const result = composeDeviceStatusNarrative(baseDevice);
    expect(result.text).toMatch(/available/i);
    expect(result.actions).toEqual([]);
  });

  it('offline + suppressed → suggests retry + maintenance', () => {
    const device = {
      ...baseDevice,
      availability_status: 'offline',
      lifecycle_policy_summary: {
        state: 'suppressed',
        label: 'Suppressed',
        detail: 'Node restart failed',
        backoff_until: null,
      },
      health_summary: { healthy: false, summary: 'Disconnected', last_checked_at: null },
      needs_attention: true,
    } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('offline');
    expect(result.text.toLowerCase()).toContain('admin');
    expect(result.actions.map((a) => a.kind)).toEqual(
      expect.arrayContaining(['retry', 'maintenance']),
    );
  });

  it('offline + backoff → engine retrying, single retry action', () => {
    const device = {
      ...baseDevice,
      availability_status: 'offline',
      lifecycle_policy_summary: {
        state: 'backoff',
        label: 'Backing off',
        detail: null,
        backoff_until: new Date(Date.now() + 2 * 60_000).toISOString(),
      },
    } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('offline');
    expect(result.text.toLowerCase()).toContain('next');
    expect(result.actions.map((a) => a.kind)).toEqual(['retry']);
  });

  it('setup required → setup action', () => {
    const device = {
      ...baseDevice,
      readiness_state: 'setup_required',
      missing_setup_fields: ['manufacturer', 'model'],
      needs_attention: true,
    } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('setup');
    expect(result.text).toContain('manufacturer');
    expect(result.actions.map((a) => a.kind)).toEqual(['setup']);
  });

  it('verification required → verify action', () => {
    const device = {
      ...baseDevice,
      readiness_state: 'verification_required',
      needs_attention: true,
    } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('verification');
    expect(result.actions.map((a) => a.kind)).toEqual(['verify']);
  });

  it('busy → status describes session, no actions', () => {
    const device = { ...baseDevice, availability_status: 'busy' } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('busy');
    expect(result.actions).toEqual([]);
  });

  it('reserved → no actions', () => {
    const device = { ...baseDevice, availability_status: 'reserved' } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('reserved');
    expect(result.actions).toEqual([]);
  });

  it('maintenance → exit-maintenance action', () => {
    const device = { ...baseDevice, availability_status: 'maintenance' } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('maintenance');
    expect(result.actions.map((a) => a.kind)).toEqual(['exit-maintenance']);
    expect(result.actions[0]?.label.toLowerCase()).toContain('maintenance');
  });

  it('plain offline (no lifecycle drama) → single retry', () => {
    const device = {
      ...baseDevice,
      availability_status: 'offline',
    } as unknown as DeviceRead;
    const result = composeDeviceStatusNarrative(device);
    expect(result.text.toLowerCase()).toContain('offline');
    expect(result.actions.map((a) => a.kind)).toEqual(['retry']);
  });
});
