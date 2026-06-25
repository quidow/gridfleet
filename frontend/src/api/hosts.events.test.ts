import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({
  default: {
    get: vi.fn(),
  },
}));

import api from './client';
import { fetchHostEvents } from './hosts';

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
