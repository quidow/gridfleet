import { describe, expect, it } from 'vitest';
import { qk } from './queryKeys';

describe('queryKeys', () => {
  it('parameterized keys start with their domain root so prefix invalidation matches', () => {
    expect(qk.devices.list({ status: 'available' }).slice(0, 1)).toEqual([...qk.devices.root]);
    expect(qk.device.detail('d1').slice(0, 1)).toEqual([...qk.device.root]);
    expect(qk.deviceHealth.byDevice('d1').slice(0, 1)).toEqual([...qk.deviceHealth.root]);
    expect(qk.runs.cursorList(undefined).slice(0, 1)).toEqual([...qk.runs.root]);
    expect(qk.sessions.cursorList(undefined).slice(0, 1)).toEqual([...qk.sessions.root]);
    expect(qk.webhooks.deliveries('w1', 10).slice(0, 1)).toEqual([...qk.webhooks.root]);
    expect(qk.hostPlugins.byHost('h1').slice(0, 1)).toEqual([...qk.hostPlugins.root]);
    expect(qk.deviceDiagnosticSnapshots.list('d1', 5).slice(0, 2)).toEqual([
      ...qk.deviceDiagnosticSnapshots.byDevice('d1'),
    ]);
  });

  it('cursor lists keep the literal "cursor" segment (useEventStream newest-page predicate depends on it)', () => {
    expect(qk.sessions.cursorList({ cursor: 'x' })[1]).toBe('cursor');
    expect(qk.runs.cursorList({ cursor: 'x' })[1]).toBe('cursor');
  });
});
