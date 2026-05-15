import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
  },
}));

import api from './client';
import { fetchHostAgentLogs, fetchHostEvents } from './hosts';

describe('fetchHostAgentLogs', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
  });

  it('GETs /hosts/:id/agent-logs with filters', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { lines: [], total: 0, has_more: false } });
    await fetchHostAgentLogs('host-1', { level: 'WARN', q: 'foo', limit: 500 });
    expect(api.get).toHaveBeenCalledWith('/hosts/host-1/agent-logs', {
      params: { level: 'WARN', q: 'foo', limit: 500 },
    });
  });
});

describe('fetchHostEvents', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset();
  });

  it('GETs /hosts/:id/events with types joined by comma', async () => {
    vi.mocked(api.get).mockResolvedValue({ data: { events: [], total: 0 } });
    await fetchHostEvents('host-1', { types: ['host.status_changed', 'host.heartbeat_lost'], limit: 50 });
    expect(api.get).toHaveBeenCalledWith('/hosts/host-1/events', {
      params: { types: 'host.status_changed,host.heartbeat_lost', limit: 50 },
    });
  });
});
